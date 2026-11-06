"""Score a reviewer configuration on the seeded-bug cases.

    python -m eval.run_eval --rules-only                  # free baseline, no API key
    ANTHROPIC_API_KEY=... python -m eval.run_eval         # full crew
    python -m eval.run_eval --agents security,correctness --report eval-report.json

Metrics:
  recall               planted bugs that got a comment within 2 lines, in the right file
  recall (reasoning)   the same, only for bugs a pattern-matcher can't find
  precision            inline comments that point at a planted bug
  noise on clean PRs   comments posted on PRs that contain no bugs
  bad locations        findings dropped because they pointed outside the diff (hallucinated lines)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from eval.cases import load_cases
from reviewcrew.agents import BRIEFS, LLMAgent, RuleAgent
from reviewcrew.coordinator import Coordinator, ReviewConfig


def build(args) -> Coordinator:
    agents: list = [RuleAgent()]
    model = ""
    if not args.rules_only:
        from reviewcrew.llm import AnthropicClient

        model = args.model
        client = AnthropicClient(os.environ.get("ANTHROPIC_API_KEY", ""), model=model)
        agents += [LLMAgent(BRIEFS[n.strip()], client) for n in args.agents.split(",")]
    return Coordinator(agents, ReviewConfig(), model=model)


def evaluate(coord: Coordinator) -> dict:
    rows, total_cost, tokens = [], 0.0, 0
    for case in load_cases():
        review = coord.review(case.diff, case.title)
        comments = review.comments + review.notes
        hit = [any(c.path == e.path and abs(c.line - e.line) <= 2 for c in comments) for e in case.expected]
        useful = [
            c for c in review.comments if any(c.path == e.path and abs(c.line - e.line) <= 2 for e in case.expected)
        ]
        rows.append(
            {
                "case": case.id,
                "expected": len(case.expected),
                "caught": sum(hit),
                "reasoning_expected": sum(e.needs_reasoning for e in case.expected),
                "reasoning_caught": sum(h for h, e in zip(hit, case.expected, strict=True) if e.needs_reasoning),
                "comments": len(review.comments),
                "useful_comments": len(useful),
                "clean": not case.expected,
                "bad_locations": review.dropped["bad_location"],
                "titles": [f"{c.path}:{c.line} {c.title}" for c in comments],
            }
        )
        total_cost += review.cost or 0.0
        tokens += review.usage.input_tokens + review.usage.output_tokens

    exp = sum(r["expected"] for r in rows)
    rexp = sum(r["reasoning_expected"] for r in rows)
    comments = sum(r["comments"] for r in rows if not r["clean"])
    summary = {
        "recall": sum(r["caught"] for r in rows) / exp,
        "recall_pattern": (sum(r["caught"] - r["reasoning_caught"] for r in rows) / (exp - rexp))
        if exp > rexp
        else None,
        "recall_reasoning": sum(r["reasoning_caught"] for r in rows) / rexp if rexp else None,
        "precision": sum(r["useful_comments"] for r in rows if not r["clean"]) / comments if comments else None,
        "noise_on_clean_prs": sum(r["comments"] for r in rows if r["clean"]),
        "bad_locations": sum(r["bad_locations"] for r in rows),
        "tokens": tokens,
        "cost_usd": round(total_cost, 4),
    }
    return {"summary": summary, "cases": rows}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rules-only", action="store_true")
    ap.add_argument("--agents", default="security,correctness,tests,performance")
    ap.add_argument("--model", default="claude-sonnet-5-5")
    ap.add_argument("--report", type=Path)
    ap.add_argument("--min-recall-pattern", type=float, default=None, help="CI gate for the rule agent")
    args = ap.parse_args(argv)

    result = evaluate(build(args))
    s = result["summary"]
    label = "rules only" if args.rules_only else f"rules + {args.agents} ({args.model})"
    print(f"Configuration: {label}\n")
    print("| case | planted | caught | comments |")
    print("|---|---|---|---|")
    for r in result["cases"]:
        print(f"| {r['case']} | {r['expected']} | {r['caught']} | {r['comments']} |")

    def pct(v):
        return "-" if v is None else f"{v:.0%}"

    print(
        f"\nrecall {pct(s['recall'])} (pattern bugs {pct(s['recall_pattern'])}, reasoning bugs "
        f"{pct(s['recall_reasoning'])}) · precision {pct(s['precision'])} · "
        f"noise on clean PRs: {s['noise_on_clean_prs']} comments · bad locations dropped: {s['bad_locations']}"
    )
    if s["tokens"]:
        print(f"tokens {s['tokens']:,} · cost ≈ ${s['cost_usd']}")
    if args.report:
        args.report.write_text(json.dumps(result, indent=2))
    if args.min_recall_pattern is not None and (s["recall_pattern"] or 0) < args.min_recall_pattern:
        print(f"FAIL: pattern recall {s['recall_pattern']:.2f} < {args.min_recall_pattern}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
