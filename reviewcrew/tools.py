"""Tools agents can call to look beyond the diff: read a file, search the repo.

Reviewing a diff in isolation misses most real bugs ("is this function's return value checked by
its callers?"). These tools give agents read-only access to the checked-out repository, with
path-traversal protection and output limits so one call can't blow the token budget.
"""

from __future__ import annotations

import re
from pathlib import Path

MAX_READ_LINES = 200
MAX_SEARCH_RESULTS = 30
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build"}

TOOL_SPECS = [
    {
        "name": "read_file",
        "description": "Read lines from a file in the repository (at the PR's head commit). "
        "Use it to see code around a change, callers, or definitions the diff doesn't show.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repository-relative path"},
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
            },
            "required": ["path"],
        },
    },
    {
        "name": "search_code",
        "description": "Search the repository for a regular expression. Returns matching lines as path:line: text. "
        "Use it to find callers, other uses of a function, or existing tests.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path_glob": {"type": "string", "description": "Optional glob, e.g. '**/*.py'"},
            },
            "required": ["pattern"],
        },
    },
]


class RepoTools:
    def __init__(self, root: str | Path | None):
        self.root = Path(root).resolve() if root else None

    @property
    def available(self) -> bool:
        return self.root is not None and self.root.is_dir()

    def _safe(self, rel: str) -> Path | None:
        if not self.available:
            return None
        p = (self.root / rel).resolve()
        return p if p.is_relative_to(self.root) and p.is_file() else None

    def read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> str:
        p = self._safe(path)
        if p is None:
            return f"error: {path} not found"
        lines = p.read_text(errors="replace").splitlines()
        start = max(1, int(start_line))
        end = min(len(lines), int(end_line) if end_line else start + MAX_READ_LINES - 1, start + MAX_READ_LINES - 1)
        body = "\n".join(f"{i:>5} {lines[i - 1]}" for i in range(start, end + 1))
        more = f"\n... ({len(lines) - end} more lines)" if end < len(lines) else ""
        return f"{path} lines {start}-{end} of {len(lines)}:\n{body}{more}"

    def search_code(self, pattern: str, path_glob: str = "**/*") -> str:
        if not self.available:
            return "error: repository not available"
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            return f"error: bad regex: {exc}"
        hits = []
        for f in sorted(self.root.glob(path_glob or "**/*")):
            if not f.is_file() or any(part in SKIP_DIRS for part in f.relative_to(self.root).parts):
                continue
            if f.stat().st_size > 500_000:
                continue
            try:
                for i, line in enumerate(f.read_text().splitlines(), 1):
                    if rx.search(line):
                        hits.append(f"{f.relative_to(self.root).as_posix()}:{i}: {line.strip()[:200]}")
                        if len(hits) >= MAX_SEARCH_RESULTS:
                            return "\n".join(hits) + "\n... (more results truncated)"
            except (UnicodeDecodeError, OSError):
                continue
        return "\n".join(hits) or "no matches"

    def call(self, name: str, args: dict) -> str:
        try:
            if name == "read_file":
                return self.read_file(args["path"], args.get("start_line", 1), args.get("end_line"))
            if name == "search_code":
                return self.search_code(args["pattern"], args.get("path_glob", "**/*"))
        except (KeyError, TypeError, ValueError) as exc:
            return f"error: bad arguments: {exc}"
        return f"error: unknown tool {name}"
