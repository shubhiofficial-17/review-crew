"""A specialist reviewer: an LLM with a focused brief, repo tools, and a structured way to answer.

The agent loop:
  1. The model sees the diff (with line numbers) and its brief.
  2. It may call read_file / search_code to gather context (bounded by max_tool_calls).
  3. It finishes by calling submit_findings with a JSON list. Using a tool for the final answer
     gives schema-shaped output instead of hoping free text parses.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

from reviewcrew.diff import FileDiff
from reviewcrew.llm import LLMClient
from reviewcrew.models import AgentResult, Finding, Severity, Usage
from reviewcrew.tools import TOOL_SPECS, RepoTools

SUBMIT_TOOL = {
    "name": "submit_findings",
    "description": "Submit your final review. Call exactly once, when done. An empty list is a valid answer.",
    "input_schema": {
        "type": "object",
        "properties": {
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "line": {"type": "integer", "description": "New-side line number shown in the diff"},
                        "severity": {"type": "string", "enum": ["nit", "minor", "major", "critical"]},
                        "title": {"type": "string", "description": "Under 80 characters"},
                        "body": {"type": "string", "description": "What is wrong, why it matters, how to fix"},
                        "suggestion": {"type": "string", "description": "Optional replacement for that line"},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                    "required": ["path", "line", "severity", "title", "body", "confidence"],
                },
            }
        },
        "required": ["findings"],
    },
}

COMMON_RULES = """
Rules for every finding:
- Only comment on lines that were ADDED in this PR (marked +). Use the line number shown in the diff.
- Report real problems with a concrete consequence. No praise, no restating the code, no generic advice.
- Style preferences are not findings unless they cause bugs or real confusion.
- If you are unsure, use read_file / search_code to check before reporting, or lower your confidence.
- Prefer returning nothing over returning noise. An empty list is a good answer for a clean diff.
- Finish by calling submit_findings exactly once."""


@dataclass(frozen=True)
class Brief:
    name: str
    category: str
    focus: str


BRIEFS = {
    "security": Brief(
        "security",
        "security",
        """You are a senior application security reviewer.
Look for: injection (SQL, shell, template, path traversal), authentication and authorisation gaps
(missing permission checks, IDOR), secrets in code, unsafe deserialisation, SSRF, weak crypto,
sensitive data in logs, missing input validation at trust boundaries.""",
    ),
    "correctness": Brief(
        "correctness",
        "correctness",
        """You are a meticulous senior engineer reviewing for bugs.
Look for: logic errors, off-by-one, wrong conditions, unhandled None/empty/error cases, resource leaks,
race conditions, incorrect error handling, broken invariants, API misuse, changes that break callers
(use search_code to find them).""",
    ),
    "tests": Brief(
        "tests",
        "tests",
        """You review test coverage and test quality.
Look for: new behaviour or branches with no test, tests that can't fail (no assertions, asserting mocks
only), tests coupled to implementation details, missing edge cases for the code that changed.
Use search_code to check whether tests exist elsewhere before saying they are missing.""",
    ),
    "performance": Brief(
        "performance",
        "performance",
        """You review for performance problems that matter at scale.
Look for: N+1 queries, queries or I/O inside loops, unbounded memory growth, quadratic algorithms on
inputs that can be large, missing pagination, blocking calls in async code. Ignore micro-optimisations.""",
    ),
}


def _parse_findings(payload: dict | str, agent: str, category: str) -> list[Finding]:
    if isinstance(payload, str):  # fallback: model answered in text; pull out the JSON
        m = re.search(r"\{.*\}|\[.*\]", payload, re.S)
        if not m:
            return []
        try:
            payload = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
    items = payload.get("findings", []) if isinstance(payload, dict) else payload
    out = []
    for it in items if isinstance(items, list) else []:
        try:
            out.append(
                Finding(
                    path=str(it["path"]).removeprefix("b/"),
                    line=int(it["line"]),
                    title=str(it["title"])[:120],
                    body=str(it["body"]),
                    category=category,
                    severity=Severity.parse(it.get("severity", "minor")),
                    confidence=float(it.get("confidence", 0.6)),
                    suggestion=it.get("suggestion") or None,
                    agents=[agent],
                )
            )
        except (KeyError, TypeError, ValueError):
            continue  # drop malformed items rather than failing the whole review
    return out


class LLMAgent:
    def __init__(
        self,
        brief: Brief,
        client: LLMClient,
        tools: RepoTools | None = None,
        max_tool_calls: int = 8,
        max_tokens: int = 4096,
    ):
        self.brief, self.client, self.tools = brief, client, tools or RepoTools(None)
        self.name, self.max_tool_calls, self.max_tokens = brief.name, max_tool_calls, max_tokens

    def review(self, files: list[FileDiff], context: str, title: str = "", description: str = "") -> AgentResult:
        started, usage, tool_calls = time.perf_counter(), Usage(), 0
        tools = ([*TOOL_SPECS] if self.tools.available else []) + [SUBMIT_TOOL]
        pr = f"PR title: {title}\n\nPR description:\n{description or '(none)'}\n\n" if title or description else ""
        messages: list[dict] = [{"role": "user", "content": f"{pr}Diff to review:\n\n{context}"}]
        system = self.brief.focus + "\n" + COMMON_RULES
        try:
            for _ in range(self.max_tool_calls + 2):
                resp = self.client.chat(system, messages, tools, self.max_tokens)
                usage.add(resp.usage)
                submit = next((c for c in resp.tool_calls if c.name == "submit_findings"), None)
                if submit:
                    findings = _parse_findings(submit.input, self.name, self.brief.category)
                    return AgentResult(self.name, findings, usage, time.perf_counter() - started, None, tool_calls)
                if not resp.tool_calls:
                    findings = _parse_findings(resp.text, self.name, self.brief.category)
                    return AgentResult(self.name, findings, usage, time.perf_counter() - started, None, tool_calls)
                messages.append({"role": "assistant", "content": resp.raw_content})
                results = []
                for call in resp.tool_calls:
                    tool_calls += 1
                    over = tool_calls > self.max_tool_calls
                    out = (
                        "error: tool budget exhausted; submit your findings now"
                        if over
                        else self.tools.call(call.name, call.input)
                    )
                    results.append({"type": "tool_result", "tool_use_id": call.id, "content": out[:12_000]})
                messages.append({"role": "user", "content": results})
            return AgentResult(
                self.name, [], usage, time.perf_counter() - started, "did not submit findings", tool_calls
            )
        except Exception as exc:  # one failing agent must not sink the whole review
            return AgentResult(
                self.name, [], usage, time.perf_counter() - started, f"{type(exc).__name__}: {exc}", tool_calls
            )
