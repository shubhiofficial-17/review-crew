from eval.cases import load_cases
from eval.run_eval import main


def test_cases_build_with_correct_lines():
    for case in load_cases():
        for e in case.expected:
            assert e.contains in case.files[e.path][1].splitlines()[e.line - 1]


def test_rules_baseline_gate():
    assert main(["--rules-only", "--min-recall-pattern", "1.0"]) == 0
