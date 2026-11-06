"""Render a Review as GitHub review comments and a markdown summary."""

from __future__ import annotations

from reviewcrew.coordinator import Review
from reviewcrew.models import Finding, Severity

MARKER = "<!-- review-crew -->"  # lets us find and update our own summary on re-runs
ICON = {Severity.CRITICAL: "🔴", Severity.MAJOR: "🟠", Severity.MINOR: "🟡", Severity.NIT: "⚪"}


def comment_body(f: Finding) -> str:
    who = ", ".join(f.agents)
    body = f"{ICON[f.severity]} **{f.title}** · {f.category} · {f.severity.name.lower()}\n\n{f.body}"
    if f.suggestion:
        body += f"\n\n```suggestion\n{f.suggestion.rstrip()}\n```"
    return body + f"\n\n<sub>review-crew: {who} · confidence {f.confidence:.2f}</sub>"


def summary_markdown(review: Review) -> str:
    counts = {s: sum(1 for f in review.comments if f.severity == s) for s in Severity}
    lines = [MARKER, "## review-crew"]
    if not review.comments and not review.notes:
        lines.append("No issues found in the reviewed changes. ✅")
    else:
        tally = " · ".join(f"{ICON[s]} {n} {s.name.lower()}" for s, n in sorted(counts.items(), reverse=True) if n)
        lines.append(tally or "PR-level notes only.")
        if review.comments:
            lines += ["", "| | Finding | Location | Agents |", "|---|---|---|---|"]
            for f in review.comments:
                lines.append(f"| {ICON[f.severity]} | {f.title} | `{f.path}:{f.line}` | {', '.join(f.agents)} |")
    for n in review.notes:
        lines += ["", f"**{n.title}**: {n.body}"]
    u = review.usage
    n = len(review.reviewed_files)
    stats = [f"{n} file{'s' if n != 1 else ''} reviewed", f"{review.seconds:.1f}s"]
    if u.calls:
        stats.append(f"{u.input_tokens:,} in / {u.output_tokens:,} out tokens over {u.calls} calls")
    if review.cost is not None:
        stats.append(f"≈ ${review.cost:.3f}")
    lines += ["", f"<sub>{' · '.join(stats)}</sub>"]
    if review.skipped_files:
        lines.append(f"<sub>Not reviewed (over budget): {', '.join(review.skipped_files[:10])}</sub>")
    errors = [r for r in review.agent_results if r.error]
    if errors:
        lines.append("<sub>⚠️ " + "; ".join(f"{r.agent} failed: {r.error[:80]}" for r in errors) + "</sub>")
    return "\n".join(lines)


def github_review_payload(review: Review, commit_sha: str) -> dict:
    return {
        "commit_id": commit_sha,
        "event": "COMMENT",  # never auto-approve or block; humans decide
        "body": summary_markdown(review),
        "comments": [
            {"path": f.path, "line": f.line, "side": "RIGHT", "body": comment_body(f)} for f in review.comments
        ],
    }
