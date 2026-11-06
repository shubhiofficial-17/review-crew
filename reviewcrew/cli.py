"""Command line entry point.

review-crew diff changes.patch                       # review a diff file
git diff main | review-crew diff -                   # review from stdin
review-crew pr owner/repo 123 --post                 # review a GitHub PR and post the review
review-crew diff changes.patch --rules-only          # no LLM, no API key, free
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from reviewcrew.agents import BRIEFS, LLMAgent, RuleAgent
from reviewcrew.coordinator import Coordinator, ReviewConfig
from reviewcrew.report import comment_body, github_review_payload, summary_markdown
from reviewcrew.tools import RepoTools


def build_coordinator(args) -> Coordinator:
    agents: list = [RuleAgent()]
    model = ""
    if not args.rules_only:
        from reviewcrew.llm import AnthropicClient

        model = args.model
        client = AnthropicClient(os.environ.get("ANTHROPIC_API_KEY", ""), model=model)
        tools = RepoTools(args.repo_dir)
        for name in args.agents.split(","):
            agents.append(LLMAgent(BRIEFS[name.strip()], client, tools, max_tool_calls=args.max_tool_calls))
    cfg = ReviewConfig(max_comments=args.max_comments, min_confidence=args.min_confidence)
    return Coordinator(agents, cfg, model=model)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="review-crew")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("diff", "pr"):
        p = sub.add_parser(name)
        if name == "diff":
            p.add_argument("path", help="diff file, or - for stdin")
        else:
            p.add_argument("repo", help="owner/name")
            p.add_argument("number", type=int)
            p.add_argument("--post", action="store_true", help="post the review to the PR")
        p.add_argument("--agents", default="security,correctness,tests,performance")
        p.add_argument("--model", default=os.environ.get("REVIEW_MODEL", "claude-sonnet-5-5"))
        p.add_argument("--rules-only", action="store_true")
        p.add_argument("--repo-dir", default=".", help="checkout the agents may read for context")
        p.add_argument("--max-comments", type=int, default=12)
        p.add_argument("--min-confidence", type=float, default=0.5)
        p.add_argument("--max-tool-calls", type=int, default=8)
        p.add_argument("--json", action="store_true", help="print machine-readable output")
    args = ap.parse_args(argv)

    title = description = ""
    if args.cmd == "diff":
        diff = sys.stdin.read() if args.path == "-" else Path(args.path).read_text()
    else:
        from reviewcrew.github import GitHub

        gh = GitHub(os.environ["GITHUB_TOKEN"])
        pr = gh.pull(args.repo, args.number)
        title, description, diff = pr["title"], pr.get("body") or "", gh.diff(args.repo, args.number)

    review = build_coordinator(args).review(diff, title, description)

    if args.json:
        print(
            json.dumps(
                {
                    "comments": [f.to_dict() for f in review.comments],
                    "notes": [f.to_dict() for f in review.notes],
                    "dropped": review.dropped,
                    "usage": vars(review.usage),
                    "cost_usd": review.cost,
                },
                indent=2,
            )
        )
    else:
        print(summary_markdown(review))
        for f in review.comments:
            print(f"\n--- {f.path}:{f.line}\n{comment_body(f)}")

    if args.cmd == "pr" and args.post:
        gh.post_review(args.repo, args.number, github_review_payload(review, pr["head"]["sha"]))
        print(f"\nPosted review to {args.repo}#{args.number}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
