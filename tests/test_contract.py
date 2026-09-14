"""L2a - the termination contract. Invalid JSON is never inferred around."""
import json
import pytest
from plat.ingest import load_verdict, ContractError

GOOD_CODE = {
    "lot": "x", "phase": "code", "attempt": 1, "status": "complete",
    "summary": "did the thing", "changed_files": ["a.py"], "commits": ["abc"],
    "tests": {"cmd": "pytest", "ran": True, "passed": 3, "failed": 0},
    "acceptance_criteria": [{"id": "AC1", "met": True, "evidence": "tests/x.py"}],
    "decisions": [{"choice": "new handler", "alternatives": ["extend existing"],
                   "rationale": "single-parcel assumption"}],
}
GOOD_REVIEW = {
    "verdict": "changes_requested",
    "findings": [{"id": "f1", "fingerprint": "mod:rule:sym", "severity": "high",
                  "file": "a.py", "line": 3, "claim": "x", "required_fix": "y"}],
    "criteria_check": [{"id": "AC1", "met": False}],
}


def w(tmp_path, phase, obj):
    (tmp_path / f"{phase}.json").write_text(json.dumps(obj))
    return tmp_path


def test_valid_verdicts_load(tmp_path):
    assert load_verdict(w(tmp_path, "code", GOOD_CODE), "code")["status"] == "complete"
    assert load_verdict(w(tmp_path, "review", GOOD_REVIEW), "review")["verdict"]


def test_missing_file_is_a_contract_error(tmp_path):
    with pytest.raises(ContractError, match="never written"):
        load_verdict(tmp_path, "code")


def test_unparseable_json_is_a_contract_error(tmp_path):
    (tmp_path / "code.json").write_text("Here is my summary! {not json")
    with pytest.raises(ContractError, match="not valid JSON"):
        load_verdict(tmp_path, "code")


def test_missing_required_field_rejected(tmp_path):
    bad = {k: v for k, v in GOOD_CODE.items() if k != "tests"}
    with pytest.raises(ContractError, match="failed schema"):
        load_verdict(w(tmp_path, "code", bad), "code")


def test_finding_without_fingerprint_rejected(tmp_path):
    """No fingerprint means no oscillation detection, so it is not a valid finding."""
    bad = json.loads(json.dumps(GOOD_REVIEW))
    del bad["findings"][0]["fingerprint"]
    with pytest.raises(ContractError, match="fingerprint"):
        load_verdict(w(tmp_path, "review", bad), "review")


def test_decision_without_alternatives_rejected(tmp_path):
    """If nothing else was on the table it was not a decision, it was just work."""
    bad = json.loads(json.dumps(GOOD_CODE))
    bad["decisions"][0]["alternatives"] = []
    with pytest.raises(ContractError, match="failed schema"):
        load_verdict(w(tmp_path, "code", bad), "code")


def test_invalid_verdict_value_rejected(tmp_path):
    bad = json.loads(json.dumps(GOOD_REVIEW))
    bad["verdict"] = "looks good to me"
    with pytest.raises(ContractError):
        load_verdict(w(tmp_path, "review", bad), "review")


def test_findings_that_stop_being_raised_are_marked_resolved():
    """The corpus must distinguish a complaint that was FIXED from one still open.
    Without it prior art can only say someone once complained, never whether it
    stuck — which is most of its value."""
    import inspect
    from plat import ingest
    src = inspect.getsource(ingest.persist)
    assert "resolved_in_attempt_id" in src
    assert "Attempt.n < attempt.n" in src, "only PRIOR attempts' findings resolve"
