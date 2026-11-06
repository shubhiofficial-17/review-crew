from reviewcrew.agents import BRIEFS, LLMAgent, RuleAgent
from reviewcrew.diff import parse_diff
from reviewcrew.llm import LLMResponse, ScriptedClient, text_response, tool_response
from reviewcrew.models import Severity, Usage
from reviewcrew.tools import RepoTools

DIFF = """diff --git a/svc.py b/svc.py
new file mode 100644
--- /dev/null
+++ b/svc.py
@@ -0,0 +1,4 @@
+def get(user_id, cache={}):
+    row = db.execute(f"SELECT * FROM t WHERE id={user_id}")
+    API_TOKEN = "abcdefghijklmnop"
+    return row
"""


def finding(path="svc.py", line=2, sev="critical", title="SQL injection", conf=0.9, **kw):
    return {"path": path, "line": line, "severity": sev, "title": title, "body": "explain", "confidence": conf, **kw}


def test_rule_agent_flags_added_lines():
    res = RuleAgent().review(parse_diff(DIFF))
    titles = {(f.line, f.title) for f in res.findings}
    assert (1, "Mutable default argument") in titles
    assert (2, "SQL built with string formatting") in titles
    assert (3, "Hardcoded credential") in titles


def test_agent_uses_tools_then_submits(tmp_path):
    (tmp_path / "db.py").write_text("def execute(sql):\n    return run(sql)\n")
    client = ScriptedClient(
        [
            tool_response("search_code", {"pattern": "def execute"}, "c1"),
            tool_response("read_file", {"path": "db.py"}, "c2"),
            tool_response("submit_findings", {"findings": [finding()]}, "c3"),
        ]
    )
    agent = LLMAgent(BRIEFS["security"], client, RepoTools(tmp_path))
    res = agent.review(parse_diff(DIFF), context="ctx")
    assert res.error is None and res.tool_calls == 2
    assert [(f.line, f.severity, f.agents) for f in res.findings] == [(2, Severity.CRITICAL, ["security"])]
    # The tool results were sent back to the model with matching ids
    last_user = client.calls[2]["messages"][-1]["content"]
    assert last_user[0]["tool_use_id"] == "c2" and "def execute" in last_user[0]["content"]
    assert res.usage.calls == 3


def test_agent_falls_back_to_json_in_text_and_drops_malformed_items():
    text = (
        'Here you go:\n```json\n{"findings": ['
        + '{"path": "svc.py", "line": 2, "severity": "major", "title": "t", "body": "b", "confidence": 0.8},'
        + '{"path": "svc.py", "line": "not a number", "title": "bad"}]}\n```'
    )
    res = LLMAgent(BRIEFS["correctness"], ScriptedClient([text_response(text)])).review([], context="ctx")
    assert len(res.findings) == 1 and res.findings[0].severity == Severity.MAJOR


def test_agent_tool_budget_is_enforced():
    client = ScriptedClient(
        [tool_response("search_code", {"pattern": "x"}, f"c{i}") for i in range(3)]
        + [tool_response("submit_findings", {"findings": []}, "done")]
    )
    res = LLMAgent(BRIEFS["tests"], client, RepoTools("."), max_tool_calls=2).review([], context="ctx")
    assert res.tool_calls == 3 and res.findings == [] and res.error is None
    assert "budget exhausted" in client.calls[3]["messages"][-1]["content"][0]["content"]


def test_agent_failure_is_contained():
    class Boom:
        model = "x"

        def chat(self, *a, **k):
            raise TimeoutError("api down")

    res = LLMAgent(BRIEFS["security"], Boom()).review([], context="ctx")
    assert res.findings == [] and "TimeoutError" in res.error


def test_repo_tools_block_path_traversal(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    tools = RepoTools(tmp_path)
    assert "x = 1" in tools.read_file("a.py")
    assert tools.read_file("../../etc/passwd").startswith("error")
    assert tools.call("rm_rf", {}).startswith("error")
    assert tools.search_code("(").startswith("error: bad regex")


def test_unused_imports_keep_linters_quiet():
    assert LLMResponse and Usage
