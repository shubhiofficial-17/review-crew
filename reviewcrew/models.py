from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import IntEnum


class Severity(IntEnum):
    NIT = 1
    MINOR = 2
    MAJOR = 3
    CRITICAL = 4

    @classmethod
    def parse(cls, value: str | int) -> Severity:
        if isinstance(value, int):
            return cls(max(1, min(4, value)))
        return {
            "nit": cls.NIT,
            "info": cls.NIT,
            "low": cls.MINOR,
            "minor": cls.MINOR,
            "medium": cls.MAJOR,
            "major": cls.MAJOR,
            "high": cls.CRITICAL,
            "critical": cls.CRITICAL,
        }.get(str(value).lower(), cls.MINOR)


@dataclass
class Finding:
    path: str
    line: int
    title: str
    body: str
    category: str  # security | correctness | tests | performance | style
    severity: Severity = Severity.MINOR
    confidence: float = 0.7
    suggestion: str | None = None  # replacement code for the line, rendered as a GitHub suggestion
    agents: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["severity"] = self.severity.name.lower()
        return d


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.calls += other.calls


@dataclass
class AgentResult:
    agent: str
    findings: list[Finding]
    usage: Usage
    seconds: float
    error: str | None = None
    tool_calls: int = 0
