"""Unified diff parsing.

Review comments can only be anchored to lines that exist in the diff, so we keep, for every
file, the set of added/context line numbers on the new side. Findings pointing anywhere else
are rejected later (that's how hallucinated line numbers get caught).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")

TEST_PATTERNS = (
    re.compile(r"(^|/)tests?/"),
    re.compile(r"(^|/)test_[^/]+\.py$"),
    re.compile(r"_test\.(py|go)$"),
    re.compile(r"\.(test|spec)\.[jt]sx?$"),
)
DOC_SUFFIXES = {".md", ".rst", ".txt"}
LOCK_FILES = {"package-lock.json", "yarn.lock", "poetry.lock", "uv.lock", "pnpm-lock.yaml", "Cargo.lock", "go.sum"}


@dataclass
class Hunk:
    old_start: int
    new_start: int
    header: str
    lines: list[tuple[str, int | None, str]] = field(default_factory=list)  # (op, new_lineno, text)


@dataclass
class FileDiff:
    path: str
    old_path: str | None = None
    status: str = "modified"  # added | deleted | modified | renamed
    hunks: list[Hunk] = field(default_factory=list)
    binary: bool = False

    @property
    def added_lines(self) -> dict[int, str]:
        return {n: t for h in self.hunks for op, n, t in h.lines if op == "+" and n is not None}

    @property
    def commentable_lines(self) -> set[int]:
        return {n for h in self.hunks for op, n, _ in h.lines if op in "+ " and n is not None}

    @property
    def additions(self) -> int:
        return sum(1 for h in self.hunks for op, _, _ in h.lines if op == "+")

    @property
    def deletions(self) -> int:
        return sum(1 for h in self.hunks for op, _, _ in h.lines if op == "-")

    @property
    def is_test(self) -> bool:
        return any(p.search(self.path) for p in TEST_PATTERNS)

    @property
    def is_reviewable(self) -> bool:
        name = PurePosixPath(self.path)
        return not (self.binary or self.status == "deleted" or name.name in LOCK_FILES)

    @property
    def is_docs(self) -> bool:
        return PurePosixPath(self.path).suffix.lower() in DOC_SUFFIXES

    def render(self, max_lines: int | None = None) -> str:
        """The diff for this file with new-side line numbers, which is what agents see."""
        out = [f"### {self.path} ({self.status}, +{self.additions}/-{self.deletions})"]
        count = 0
        for h in self.hunks:
            out.append(f"@@ new line {h.new_start} @@ {h.header.strip()}".rstrip())
            for op, n, text in h.lines:
                if max_lines is not None and count >= max_lines:
                    out.append("... (truncated)")
                    return "\n".join(out)
                num = f"{n:>5}" if n is not None else "     "
                out.append(f"{num} {op}{text}")
                count += 1
        return "\n".join(out)


def parse_diff(text: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    hunk: Hunk | None = None
    new_no = old_left = new_left = 0
    for line in text.splitlines():
        # Inside a hunk, the header's line counts tell us exactly how many lines belong to it,
        # so a removed line like "-- comment" can't be mistaken for a "--- a/file" header.
        if hunk is not None and (old_left > 0 or new_left > 0):
            op, body = line[:1] or " ", line[1:]
            if op == "\\":
                continue
            if op == "-":
                hunk.lines.append(("-", None, body))
                old_left -= 1
            elif op in ("+", " "):
                hunk.lines.append((op, new_no, body))
                new_no += 1
                new_left -= 1
                if op == " ":
                    old_left -= 1
            continue
        if line.startswith("\\"):
            continue
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            cur = FileDiff(path=m.group(2) if m else line.split()[-1], old_path=m.group(1) if m else None)
            files.append(cur)
            hunk = None
            continue
        if cur is None:
            continue
        if line.startswith("new file mode"):
            cur.status = "added"
        elif line.startswith("deleted file mode"):
            cur.status = "deleted"
        elif line.startswith("rename to "):
            cur.status, cur.path = "renamed", line[len("rename to ") :]
        elif line.startswith(("Binary files", "GIT binary patch")):
            cur.binary = True
        elif line.startswith("+++ ") and not line.startswith("+++ /dev/null"):
            cur.path = line[4:].removeprefix("b/")
        elif m := _HUNK.match(line):
            hunk = Hunk(int(m.group(1)), int(m.group(3)), m.group(5))
            cur.hunks.append(hunk)
            new_no = int(m.group(3))
            old_left = int(m.group(2)) if m.group(2) is not None else 1
            new_left = int(m.group(4)) if m.group(4) is not None else 1
    return files
