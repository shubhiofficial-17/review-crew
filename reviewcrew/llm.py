"""LLM clients with tool calling.

`chat()` takes Anthropic-style messages and tool definitions and returns a normalised response:
text, tool calls and token usage. `AnthropicClient` speaks the Messages API directly over HTTP;
`ScriptedClient` replays canned responses, which is how the agent loop is tested offline.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from reviewcrew.models import Usage


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall]
    usage: Usage
    raw_content: list[dict] = field(default_factory=list)  # to echo back as the assistant turn


class LLMClient(Protocol):
    model: str

    def chat(self, system: str, messages: list[dict], tools: list[dict], max_tokens: int = 2048) -> LLMResponse: ...


class AnthropicClient:
    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-5-5",
        base_url: str = "https://api.anthropic.com",
        max_retries: int = 4,
    ):
        import httpx

        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY is not set")
        self.model, self.max_retries = model, max_retries
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=180,
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        )

    def chat(self, system: str, messages: list[dict], tools: list[dict], max_tokens: int = 2048) -> LLMResponse:
        body = {"model": self.model, "max_tokens": max_tokens, "system": system, "messages": messages}
        if tools:
            body["tools"] = tools
        for attempt in range(self.max_retries + 1):
            resp = self._http.post("/v1/messages", json=body)
            if resp.status_code in (429, 500, 502, 503, 529) and attempt < self.max_retries:
                wait = float(resp.headers.get("retry-after", 2**attempt))
                time.sleep(min(wait, 30))  # rate limited / overloaded: back off and retry
                continue
            resp.raise_for_status()
            break
        data = resp.json()
        content = data.get("content", [])
        u = data.get("usage", {})
        return LLMResponse(
            text="".join(b.get("text", "") for b in content if b.get("type") == "text"),
            tool_calls=[
                ToolCall(b["id"], b["name"], b.get("input", {})) for b in content if b.get("type") == "tool_use"
            ],
            usage=Usage(u.get("input_tokens", 0), u.get("output_tokens", 0), 1),
            raw_content=content,
        )


class ScriptedClient:
    """Test double: each call returns the next scripted response (or the result of a callable)."""

    def __init__(self, script: list[LLMResponse | Callable[[list[dict]], LLMResponse]], model: str = "scripted"):
        self.script, self.model, self.calls = list(script), model, []

    def chat(self, system: str, messages: list[dict], tools: list[dict], max_tokens: int = 2048) -> LLMResponse:
        self.calls.append({"system": system, "messages": [dict(m) for m in messages], "tools": tools})
        if not self.script:
            raise RuntimeError("ScriptedClient ran out of responses")
        nxt = self.script.pop(0)
        return nxt(messages) if callable(nxt) else nxt


def text_response(text: str, inp: int = 100, out: int = 50) -> LLMResponse:
    return LLMResponse(text, [], Usage(inp, out, 1), [{"type": "text", "text": text}])


def tool_response(name: str, args: dict, call_id: str = "t1", inp: int = 100, out: int = 20) -> LLMResponse:
    return LLMResponse(
        "",
        [ToolCall(call_id, name, args)],
        Usage(inp, out, 1),
        [{"type": "tool_use", "id": call_id, "name": name, "input": args}],
    )


# USD per million tokens (input, output). PLACEHOLDER values: set these from your provider's current
# price list before trusting the cost figures.
PRICES = {"claude-sonnet-5-5": (3.0, 15.0), "claude-haiku-5-5": (1.0, 5.0), "claude-opus-5-5": (5.0, 25.0)}


def cost_usd(model: str, usage: Usage) -> float | None:
    if model not in PRICES:
        return None
    inp, out = PRICES[model]
    return (usage.input_tokens * inp + usage.output_tokens * out) / 1_000_000
