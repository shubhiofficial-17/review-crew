from reviewcrew.agents import BRIEFS, LLMAgent, RuleAgent
from reviewcrew.coordinator import Coordinator, ReviewConfig, build_context
from reviewcrew.diff import parse_diff
from reviewcrew.llm import ScriptedClient, tool_response
from reviewcrew.report import MARKER, github_review_payload, summary_markdown
from tests.test_agents import DIFF, finding


def crew(*finding_lists, cfg=None):
    agents = [RuleAgent()]
    for i, fl in enumerate(finding_lists):
        brief = list(BRIEFS.values())[i]
        agents.append(LLMAgent(brief, ScriptedClient([tool_response("submit_findings", {"findings": fl})])))
    return Coordinator(agents, cfg or ReviewConfig(), model="claude-sonnet-5-5")


def test_hallucinated_locations_are_dropped_and_near_misses_snapped():
    review = crew([finding(line=99, title="far away"), finding(path="nope.py"), finding(line=5, title="near")]).review(
        DIFF
    )
    titles = {(c.line, c.title) for c in review.comments}
    assert (4, "near") in titles  # line 5 doesn't exist; snapped to added line 4
    assert all(t != "far away" for _, t in titles)
    assert review.dropped["bad_location"] == 2


def test_agents_agreeing_merge_into_one_comment_with_higher_confidence():
    review = crew([finding(conf=0.7)], [finding(conf=0.6, title="Unsanitised input in SQL query")]).review(DIFF)
    sql = [c for c in review.comments if c.line == 2]
    assert len(sql) == 1
    assert set(sql[0].agents) >= {"rules", "security", "correctness"}
    assert sql[0].confidence > 0.8


def test_low_confidence_and_nits_filtered_and_comment_cap_applied():
    many = [finding(line=1 + i % 4, title=f"issue {i}", sev="major", conf=0.9, category_hint=i) for i in range(4)]
    low = [finding(line=4, title="maybe", conf=0.2), finding(line=4, title="nit", sev="nit", conf=0.9)]
    review = crew(many + low, cfg=ReviewConfig(max_comments=2)).review(DIFF)
    assert len(review.comments) == 2
    assert all(c.title not in ("maybe", "nit") for c in review.comments)
    assert review.comments[0].severity >= review.comments[1].severity


def test_context_budget_prioritises_risky_source_over_docs_and_tests():
    big = "".join(
        f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -0,0 +1,40 @@\n" + "".join(f"+line {i}\n" for i in range(40))
        for p in ["README.md", "tests/test_a.py", "auth/login.py"]
    )
    _, included, skipped = build_context(parse_diff(big), budget=1500)
    assert included[0] == "auth/login.py" and "README.md" in skipped


def test_report_and_payload():
    review = crew([finding(suggestion='row = db.execute("SELECT * FROM t WHERE id=%s", (user_id,))')]).review(DIFF)
    payload = github_review_payload(review, "abc123")
    assert payload["event"] == "COMMENT" and payload["commit_id"] == "abc123"
    sql = next(c for c in payload["comments"] if c["line"] == 2)
    assert sql["side"] == "RIGHT" and "```suggestion" in sql["body"]
    md = summary_markdown(review)
    assert md.startswith(MARKER) and "svc.py:2" in md and "tokens" in md


def test_clean_review_summary():
    clean = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -0,0 +1 @@\n+x = 1\n"
    review = crew([]).review(clean)
    assert review.comments == [] and "No issues found" in summary_markdown(review)
