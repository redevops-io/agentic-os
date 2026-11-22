from __future__ import annotations

import importlib

findings = importlib.import_module("deploy-scan.findings")
Finding = findings.Finding


def test_finding_rejects_an_unknown_severity():
    try:
        Finding(severity="catastrophic", name="x", target="y")
    except ValueError as e:
        assert "unknown severity" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_to_dict_has_every_field():
    f = Finding(severity="high", name="n", target="t", ref="r", detail="d", recommended_action="a")
    assert f.to_dict() == {"severity": "high", "name": "n", "target": "t", "ref": "r",
                            "detail": "d", "recommended_action": "a"}


def test_summarize_counts_by_severity_and_finds_the_worst():
    fs = [Finding(severity="low", name="a", target="t"), Finding(severity="critical", name="b", target="t"),
          Finding(severity="low", name="c", target="t")]
    s = findings.summarize("t", fs)
    assert s == {"target": "t", "total": 3, "by_severity": {"low": 2, "critical": 1}, "max_severity": "critical"}


def test_summarize_of_no_findings_is_a_clean_result_not_an_error():
    s = findings.summarize("t", [])
    assert s == {"target": "t", "total": 0, "by_severity": {}, "max_severity": ""}
