"""Deterministic checks that need no LLM: fast, free, and a baseline the LLM agents must beat.

They run on added lines only, so they never comment on code the PR didn't touch.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from reviewcrew.diff import FileDiff
from reviewcrew.models import AgentResult, Finding, Severity, Usage


@dataclass(frozen=True)
class Rule:
    id: str
    pattern: re.Pattern
    title: str
    body: str
    category: str
    severity: Severity
    languages: tuple[str, ...] = (".py",)
    confidence: float = 0.8


def _r(p: str) -> re.Pattern:
    return re.compile(p)


RULES = [
    Rule(
        "hardcoded-secret",
        _r(r"""(?i)\b\w*(api[_-]?key|secret|password|passwd|token)\w*\s*[:=]\s*['"][^'"\s]{8,}['"]"""),
        "Hardcoded credential",
        "A secret appears to be committed in source. Load it from the environment or a "
        "secret manager, and rotate this value since it is now in git history.",
        "security",
        Severity.CRITICAL,
        (".py", ".js", ".ts", ".go", ".java", ".yml", ".yaml", ".json", ".env"),
        0.75,
    ),
    Rule(
        "aws-key",
        _r(r"\bAKIA[0-9A-Z]{16}\b"),
        "AWS access key committed",
        "This looks like an AWS access key ID. Revoke it and load credentials from the environment.",
        "security",
        Severity.CRITICAL,
        ("",),
        0.95,
    ),
    Rule(
        "private-key",
        _r(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"),
        "Private key committed",
        "A private key is being added to the repository.",
        "security",
        Severity.CRITICAL,
        ("",),
        0.95,
    ),
    Rule(
        "sql-injection",
        _r(r"""\.execute\w*\(\s*(f['"]|['"][^'"]*['"]\s*(%|\.format\(|\+))"""),
        "SQL built with string formatting",
        "Building SQL with f-strings, % or + lets user input change the query "
        "(SQL injection). Pass values as query parameters instead.",
        "security",
        Severity.CRITICAL,
    ),
    Rule(
        "shell-true",
        _r(r"subprocess\.\w+\(.*shell\s*=\s*True"),
        "subprocess with shell=True",
        "With shell=True, any user-controlled part of the command can inject shell syntax. Pass an argument "
        "list without shell=True.",
        "security",
        Severity.MAJOR,
        confidence=0.7,
    ),
    Rule(
        "eval",
        _r(r"(?<![\w.])(eval|exec)\("),
        "eval/exec on dynamic input",
        "eval/exec runs arbitrary code. Use ast.literal_eval or explicit parsing.",
        "security",
        Severity.MAJOR,
        confidence=0.65,
    ),
    Rule(
        "pickle",
        _r(r"pickle\.loads?\("),
        "Unpickling data",
        "pickle can execute code while loading. Never unpickle data from an untrusted source; prefer JSON.",
        "security",
        Severity.MAJOR,
        confidence=0.6,
    ),
    Rule(
        "yaml-load",
        _r(r"yaml\.load\((?![^)]*Loader\s*=\s*yaml\.SafeLoader)"),
        "yaml.load without SafeLoader",
        "yaml.load can construct arbitrary objects. Use yaml.safe_load.",
        "security",
        Severity.MAJOR,
    ),
    Rule(
        "tls-off",
        _r(r"verify\s*=\s*False"),
        "TLS certificate verification disabled",
        "verify=False accepts any certificate, allowing man-in-the-middle attacks.",
        "security",
        Severity.MAJOR,
    ),
    Rule(
        "weak-hash",
        _r(r"hashlib\.(md5|sha1)\("),
        "Weak hash function",
        "MD5/SHA-1 are broken for security purposes. For passwords use bcrypt/argon2; otherwise SHA-256.",
        "security",
        Severity.MINOR,
        confidence=0.5,
    ),
    Rule(
        "bare-except",
        _r(r"^\s*except\s*:\s*(pass)?\s*$"),
        "Bare except",
        "A bare except also catches KeyboardInterrupt/SystemExit and hides real errors. Catch specific "
        "exceptions and log them.",
        "correctness",
        Severity.MINOR,
        confidence=0.7,
    ),
    Rule(
        "mutable-default",
        _r(r"def \w+\(.*=\s*(\[\]|\{\}|set\(\))"),
        "Mutable default argument",
        "Default values are created once and shared between calls, so mutations leak across calls. Use None "
        "and create the object inside the function.",
        "correctness",
        Severity.MAJOR,
        confidence=0.85,
    ),
    Rule(
        "debug-on",
        _r(r"(?i)\bdebug\s*=\s*True\b"),
        "Debug mode enabled",
        "Debug mode can expose stack traces and an interactive console in production.",
        "security",
        Severity.MINOR,
        confidence=0.5,
    ),
]


class RuleAgent:
    name = "rules"

    def __init__(self, rules: list[Rule] | None = None):
        self.rules = rules or RULES

    def review(self, files: list[FileDiff], **_) -> AgentResult:
        started = time.perf_counter()
        findings: list[Finding] = []
        for f in files:
            if not f.is_reviewable or f.is_docs:
                continue
            for line_no, text in f.added_lines.items():
                for rule in self.rules:
                    if rule.languages != ("",) and not f.path.endswith(rule.languages):
                        continue
                    if rule.pattern.search(text):
                        findings.append(
                            Finding(
                                f.path,
                                line_no,
                                rule.title,
                                rule.body,
                                rule.category,
                                rule.severity,
                                rule.confidence,
                                agents=[self.name],
                            )
                        )
        findings += self._missing_tests(files)
        return AgentResult(self.name, findings, Usage(), time.perf_counter() - started)

    def _missing_tests(self, files: list[FileDiff]) -> list[Finding]:
        """Source changed meaningfully but no test file touched: one PR-level note."""
        src = [
            f
            for f in files
            if f.is_reviewable and not f.is_test and not f.is_docs and f.path.endswith(".py") and f.additions >= 15
        ]
        if not src or any(f.is_test for f in files):
            return []
        biggest = max(src, key=lambda f: f.additions)
        return [
            Finding(
                biggest.path,
                0,
                "No tests changed",
                f"{sum(f.additions for f in src)} lines of source changed across {len(src)} file(s) "
                "without any test changes. Consider adding tests for the new behaviour.",
                "tests",
                Severity.MINOR,
                0.6,
                agents=[self.name],
            )
        ]
