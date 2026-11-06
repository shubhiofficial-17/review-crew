"""Runs the review crew and turns raw agent output into a review worth reading.

Pipeline: parse diff → prioritise files into a token budget → run agents in parallel →
validate locations → merge duplicates across agents → filter by confidence/severity →
cap the number of comments.
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from reviewcrew.diff import FileDiff, parse_diff
from reviewcrew.llm import cost_usd
from reviewcrew.models import AgentResult, Finding, Severity, Usage

RISKY_PATH = re.compile(
    r"(?i)(auth|login|session|token|crypto|secret|password|payment|billing|admin|permission|"
    r"sql|query|db|upload|exec|shell|api|views?|routes?|handlers?|middleware)"
)


@dataclass
class ReviewConfig:
    max_context_chars: int = 60_000  # ~15k tokens of diff per agent
    max_comments: int = 12  # noise budget: reviewers stop reading after a dozen comments
    min_confidence: float = 0.5
    min_severity: Severity = Severity.MINOR
    snap_distance: int = 3  # move a finding up to 3 lines to the nearest added line
    agree_boost: float = 0.1  # confidence bonus when independent agents flag the same thing


@dataclass
class Review:
    comments: list[Finding]  # inline comments
    notes: list[Finding]  # PR-level findings (line 0)
    agent_results: list[AgentResult]
    reviewed_files: list[str]
    skipped_files: list[str]
    dropped: dict[str, int] = field(default_factory=dict)
    seconds: float = 0.0
    model: str = ""

    @property
    def usage(self) -> Usage:
        total = Usage()
        for r in self.agent_results:
            total.add(r.usage)
        return total

    @property
    def cost(self) -> float | None:
        return cost_usd(self.model, self.usage) if self.model else None


def file_priority(f: FileDiff) -> tuple:
    """Review risky, large, non-test source first when the diff doesn't fit the budget."""
    return (f.is_docs, f.is_test, not RISKY_PATH.search(f.path), -f.additions)


def build_context(files: list[FileDiff], budget: int) -> tuple[str, list[str], list[str]]:
    parts, used, included, skipped = [], 0, [], []
    for f in sorted(files, key=file_priority):
        block = f.render()
        if used + len(block) > budget:
            remaining = budget - used
            if remaining > 2_000 and not f.is_docs:  # include a truncated version rather than nothing
                block = f.render(max_lines=max(20, remaining // 80))
            else:
                skipped.append(f.path)
                continue
        parts.append(block)
        used += len(block)
        included.append(f.path)
    return "\n\n".join(parts), included, skipped


def validate_locations(findings: list[Finding], files: dict[str, FileDiff], snap: int) -> tuple[list[Finding], int]:
    """Drop findings that point at files or lines not in the diff; snap near-misses onto added lines."""
    ok, dropped = [], 0
    for fd in findings:
        f = files.get(fd.path)
        if f is None:
            dropped += 1
            continue
        if fd.line == 0 or fd.line in f.commentable_lines:
            ok.append(fd)
            continue
        added = sorted(f.added_lines)
        nearest = min(added, key=lambda n: abs(n - fd.line), default=None)
        if nearest is not None and abs(nearest - fd.line) <= snap:
            fd.line = nearest
            ok.append(fd)
        else:
            dropped += 1
    return ok, dropped


_STOP = {
    "the",
    "and",
    "for",
    "this",
    "that",
    "with",
    "from",
    "are",
    "not",
    "can",
    "use",
    "line",
    "code",
    "value",
    "should",
    "could",
    "would",
    "may",
    "might",
    "will",
    "when",
    "which",
    "into",
    "than",
    "then",
    "there",
    "here",
}


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{3,}", s.lower()) if w not in _STOP}


def _same_issue(a: Finding, b: Finding) -> bool:
    """Same file and nearby line, and plausibly the same problem.

    Different agents describe one bug differently ("SQL injection" vs "unsanitised input in query"),
    so on the *same* line one shared title word is enough; a couple of lines apart we need the same
    category or substantial word overlap.
    """
    if a.path != b.path or abs(a.line - b.line) > 2:
        return False
    ta, tb = _words(a.title), _words(b.title)
    if a.line == b.line and (a.category == b.category or ta & tb):
        return True
    if a.category == b.category:
        return True
    wa, wb = ta | _words(a.body[:200]), tb | _words(b.body[:200])
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= 0.3


def merge_duplicates(findings: list[Finding], boost: float) -> list[Finding]:
    merged: list[Finding] = []
    for fd in sorted(findings, key=lambda f: (-f.severity, -f.confidence)):
        twin = next((m for m in merged if _same_issue(m, fd)), None)
        if twin is None:
            merged.append(fd)
            continue
        new_agents = [a for a in fd.agents if a not in twin.agents]
        if new_agents:  # independent agreement raises confidence
            twin.agents += new_agents
            twin.confidence = min(1.0, max(twin.confidence, fd.confidence) + boost)
        twin.suggestion = twin.suggestion or fd.suggestion
    return merged


class Coordinator:
    def __init__(self, agents: list, config: ReviewConfig | None = None, model: str = ""):
        self.agents, self.cfg, self.model = agents, config or ReviewConfig(), model

    def review(self, diff_text: str, title: str = "", description: str = "") -> Review:
        started = time.perf_counter()
        files = [f for f in parse_diff(diff_text) if f.is_reviewable]
        by_path = {f.path: f for f in files}
        context, included, skipped = build_context(files, self.cfg.max_context_chars)
        reviewed = [by_path[p] for p in included]

        with ThreadPoolExecutor(max_workers=max(1, len(self.agents))) as pool:
            futures = [
                pool.submit(a.review, reviewed, context=context, title=title, description=description)
                for a in self.agents
            ]
            results = [f.result() for f in futures]

        raw = [fd for r in results for fd in r.findings]
        valid, bad_location = validate_locations(raw, by_path, self.cfg.snap_distance)
        merged = merge_duplicates(valid, self.cfg.agree_boost)
        kept = [f for f in merged if f.confidence >= self.cfg.min_confidence and f.severity >= self.cfg.min_severity]
        kept.sort(key=lambda f: (-f.severity, -f.confidence, f.path, f.line))
        inline = [f for f in kept if f.line > 0][: self.cfg.max_comments]
        notes = [f for f in kept if f.line == 0]
        dropped = {
            "bad_location": bad_location,
            "duplicates": len(valid) - len(merged),
            "low_confidence_or_severity": len(merged) - len(kept),
            "over_comment_cap": max(0, len([f for f in kept if f.line > 0]) - self.cfg.max_comments),
        }
        return Review(inline, notes, results, included, skipped, dropped, time.perf_counter() - started, self.model)
