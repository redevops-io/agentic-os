"""PR-D2 composition skills over the integrated products (docs/products/SKILLS_DESIGN.md).

The line these tests hold: the skills add composition, not authority. Reads leave only GETs; a product or profile
that does not exist answers `unavailable` with reason and remedy and makes no call of its own; every consolidated
row cites a product record id and at least one evidence link; the severity mappings are tables pinned here, and an
unmapped severity never becomes a row.
"""
from __future__ import annotations

import pytest

from agentic_os.products import gates
from agentic_os.products import schemas
from agentic_os.products import tools as toolsmod
from agentic_os.products.hub import ProductHub
from agentic_os.products.registry import load_registry
from agentic_os.products.tools import (AUDIT_PROFILE_DEFAULT, AUDIT_RULE_SEVERITY, CASE_SEVERITY, DEPLOY_SEVERITY,
                                       SEVERITY_RANK, ProductTools, build_registry)
from agentic_os.sidekick.capability import CapabilityDomain
from tests._products_fakes import CASE, EST, RUN, TGT, FakeQa, FakeSentinel, write_registry

NEW = {"quality.ui_audit", "quality.plan_generate", "security.deployment_scan", "findings.consolidate"}


def _env(tmp_path, qa: FakeQa, sentinel: FakeSentinel | None = None):
    """One- or two-product registry over the fakes; returns (hub, reg, extras)."""
    qa_base = qa.serve()
    extra = ""
    s_base = sentinel.serve() if sentinel else ""
    if sentinel:
        d = tmp_path / "secrets"
        extra = (f"\n  - id: edge-sentinel\n    display_name: Edge Sentinel\n    kind: security\n    api_base: {s_base}\n"
                 f"    ui_url: {s_base}/ui\n    health: {{path: /readyz, interval_s: 1, timeout_s: 2}}\n"
                 f"    contract: {{api: /api/v1, info: /api/v1/info, min_version: \"1\"}}\n    auth:\n"
                 f"      service_token_file: {d / 'service'}\n      decision_token_file: {d / 'decision'}\n"
                 f"      approval_secret_file: {d / 'secret'}\n      scheme: control_token\n")
    reg = write_registry(tmp_path, qa_base, kind="quality", pid="redevops-qa", extra_products=extra)
    hub = ProductHub(load_registry(reg))
    return hub, build_registry(hub)


@pytest.fixture
def env_qa(monkeypatch, tmp_path):
    for k in ("AGENTIC_OS_PROFILE", "AGENTIC_OS_CONTROL_TOKEN", "PROJECTS_APPS", "AGENTIC_OS_PRODUCTS"):
        monkeypatch.delenv(k, raising=False)
    fake = FakeQa()
    hub, reg = _env(tmp_path, fake)
    yield fake, hub, reg
    fake.close()


@pytest.fixture
def env_audit(monkeypatch, tmp_path):
    for k in ("AGENTIC_OS_PROFILE", "AGENTIC_OS_CONTROL_TOKEN", "PROJECTS_APPS", "AGENTIC_OS_PRODUCTS"):
        monkeypatch.delenv(k, raising=False)
    fake = FakeQa(audit=True, plans=True)
    hub, reg = _env(tmp_path, fake)
    yield fake, hub, reg
    fake.close()


@pytest.fixture
def env_duo(monkeypatch, tmp_path):
    for k in ("AGENTIC_OS_PROFILE", "AGENTIC_OS_CONTROL_TOKEN", "PROJECTS_APPS", "AGENTIC_OS_PRODUCTS"):
        monkeypatch.delenv(k, raising=False)
    qa, sent = FakeQa(), FakeSentinel()
    hub, reg = _env(tmp_path, qa, sent)
    yield qa, sent, hub, reg
    qa.close()
    sent.close()


def posts(fake):
    return [c["path"] for c in fake.calls if c.get("method") == "POST"]


def get_paths(fake):
    return {c["path"] for c in fake.calls if c.get("method") == "GET"}


def test_the_skills_are_tools_of_their_domains_and_add_no_authority(env_duo):
    qa, sent, hub, reg = env_duo
    ids = {c.capability_id for c in reg.all()}
    assert NEW <= ids
    assert not ids & schemas.FORBIDDEN_TOOLS
    assert not any(w in i for i in ids for w in ("decide_", "execute", "approve", "apply", "delete", "register"))
    for cid in NEW:
        assert schemas.INPUT_SCHEMAS[cid]["additionalProperties"] is False
        assert schemas.TOOL_CLASS[cid] in ("read", "stage", "present", "bounded_write")
    assert CapabilityDomain.FINDINGS.value == "findings"          # its own domain: it owns no product
    doms = {c.capability_id: c.domain.value for c in reg.all()}
    assert doms["findings.consolidate"] == "findings" and doms["quality.ui_audit"] == "quality"


def test_findings_tool_is_absent_when_no_product_is_registered(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTIC_OS_PRODUCTS", raising=False)
    reg_path = tmp_path / "empty.yaml"
    reg_path.write_text("schema: aos.products/v1\nprincipal: {single_owner: local-owner}\nproducts: []\n")
    ids = {c.capability_id for c in build_registry(ProductHub(load_registry(reg_path))).all()}
    assert not ids & NEW


def test_ui_audit_without_the_profile_is_an_explicit_unavailable_and_starts_nothing(env_qa):
    fake, hub, reg = env_qa
    r = ProductTools(hub).call("quality.ui_audit", {"target_id": TGT}, actor="t")
    assert r.data["status"] == "unavailable" and r.data["code"] == "profile_unavailable"
    assert AUDIT_PROFILE_DEFAULT in r.answer and "redevops-qa" in r.data["remedy"]
    assert posts(fake) == []                                       # nothing was estimated, nothing started


def test_ui_audit_with_the_profile_starts_a_normal_gated_run(env_audit):
    fake, hub, reg = env_audit
    r = ProductTools(hub).call("quality.ui_audit", {"target_id": TGT}, actor="t")
    assert r.data["executed"] is False and r.data["run_id"] == RUN and r.data["state"] == "awaiting_approval"
    assert r.data["approval"] and r.data["approval"]["kind"] == "approval"   # the existing approval machinery applies
    assert posts(fake) == ["/v1/estimates", "/v1/runs"]
    started = fake.started[0]
    assert started["profile"] == AUDIT_PROFILE_DEFAULT and started["estimate_digest"] == EST
    assert started["target_id"] == TGT and set(started) <= {"target_id", "profile", "estimate_digest", "defect"}


@pytest.mark.parametrize("bad", [{"target_id": "http://127.0.0.1:8080"}, {"target_id": TGT, "profile_id": "x/../../y"},
                                 {"target_id": TGT, "script": "alert(1)"}])
def test_ui_audit_schemas_carry_no_url_path_or_extra(env_qa, bad):
    fake, hub, reg = env_qa
    r = ProductTools(hub).call("quality.ui_audit", bad, actor="t")
    assert r.data["status"] == "invalid"
    # the readiness probe may have run; the point is the HANDLER never did: no write of any kind, no profile read
    assert posts(fake) == [] and "/v1/profiles" not in get_paths(fake)


def test_plan_generate_without_a_plan_builder_refuses_to_invent_one(env_qa):
    fake, hub, reg = env_qa
    r = ProductTools(hub).call("quality.plan_generate", {"target_id": TGT, "scope": "smoke"}, actor="t")
    assert r.data["status"] == "unavailable" and r.data["code"] == "plans_not_shipped"
    assert "composed by the service" in r.data["reason"]
    assert posts(fake) == []                                       # never a locally composed plan body


def test_plan_generate_sends_ids_only_and_echoes_the_frozen_digest(env_audit):
    fake, hub, reg = env_audit
    r = ProductTools(hub).call("quality.plan_generate",
                               {"target_id": TGT, "scope": "audit_followup", "source_run_id": RUN}, actor="t")
    assert r.data["executed"] is False and r.data["plan"]["digest"] == EST and r.data["plan"]["composed_by"] == "service"
    assert posts(fake) == ["/v1/plans"]
    body = fake.calls[-1]["body"]
    assert body == {"target_id": TGT, "scope": "audit_followup", "source_run_id": RUN}   # ids and one enum, nothing else


@pytest.mark.parametrize("bad", [{"target_id": TGT, "scope": "rm -rf"}, {"target_id": TGT, "scope": "smoke", "steps": "go"},
                                 {"target_id": TGT, "scope": "smoke", "source_run_id": "run; drop"}])
def test_plan_generate_schema_holds_the_line(env_qa, bad):
    fake, hub, reg = env_qa
    r = ProductTools(hub).call("quality.plan_generate", bad, actor="t")
    assert r.data["status"] == "invalid" and posts(fake) == []


def test_deployment_scan_is_get_only_on_the_allowlisted_planes(env_duo):
    from agentic_os.agent_gateway.external.deployment_inspection import SENTINEL_READ_ENDPOINTS
    qa, sent, hub, reg = env_duo
    r = ProductTools(hub).call("security.deployment_scan", {"product_id": "edge-sentinel"}, actor="t")
    assert r.data["connected"] is True and r.data["executed"] is False
    kinds = {f["kind"] for f in r.data["scan"]["findings"]}
    assert {"core_disconnected", "exposed_admin", "no_backup", "active_threats"} <= kinds   # derived from real answers
    assert all(f["evidence_ref"].startswith("dpev:") for f in r.data["scan"]["findings"])   # every finding backed
    assert [c["method"] for c in sent.calls].count("POST") == 0                             # a read is a read
    assert get_paths(sent) <= {"/readyz", "/api/v1/info"} | set(SENTINEL_READ_ENDPOINTS)


def test_deployment_scan_refuses_an_unregistered_product_id_without_a_call(env_duo):
    qa, sent, hub, reg = env_duo
    r = ProductTools(hub).call("security.deployment_scan", {"product_id": "not-there"}, actor="t")
    assert r.data["status"] == "refused"
    # the readiness probe may have run; no scan plane was touched and nothing was written
    planes = {"/health", "/api/activity", "/api/backups/status", "/api/network/review"}
    assert not (get_paths(sent) & planes) and posts(sent) == []


def test_consolidate_rows_are_record_backed_and_link_carrying(env_duo):
    qa, sent, hub, reg = env_duo
    qa.runs[RUN].update({"state": "finished", "outcome": "FAILED"})
    r = ProductTools(hub).call("findings.consolidate", {}, actor="t")
    rows = r.data["report"]["rows"]
    assert rows and {x["section"] for x in rows} == {"deployment", "cases", "runs"}
    for row in rows:
        assert row["record_id"] and row["links"] and all(u.startswith("http") for u in row["links"])
        assert row["severity"] in schemas.SEVERITY_ORDER
        assert row["recommended_fix"] and row["reproduction"]
    assert len(r.artifacts) == len(rows)
    assert {x["severity"] for x in rows} <= {"blocker", "major", "minor"}       # the fake carries no note-level rows
    # severity floor filters; product scope narrows sections; an unmapped severity never becomes a row
    strict = ProductTools(hub).call("findings.consolidate", {"severity_min": "major"}, actor="t")
    assert strict.data["report"]["rows"] and all(x["severity"] in ("major", "blocker") for x in strict.data["report"]["rows"])
    sent.cases[CASE]["severity"] = "legendary"                                   # not in the mapping table
    only_sec = ProductTools(hub).call("findings.consolidate", {"product_ids": ["edge-sentinel"]}, actor="t")
    secs = only_sec.data["report"]["sections"]
    assert secs["cases"]["included"] is True and "runs" not in secs
    assert not any(x["record_id"] == CASE for x in only_sec.data["report"]["rows"])


def test_consolidate_with_nothing_reachable_is_an_explicit_unavailable(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTIC_OS_PRODUCTS", raising=False)
    reg = write_registry(tmp_path, "http://127.0.0.1:9", kind="quality", pid="redevops-qa")
    hub = ProductHub(load_registry(reg))
    r = ProductTools(hub).call("findings.consolidate", {}, actor="t")
    assert r.data["status"] == "unavailable" and r.confidence == 0.0


def test_the_severity_tables_are_pinned_and_the_audit_profile_is_named():
    assert SEVERITY_RANK == {"note": 0, "minor": 1, "major": 2, "blocker": 3}
    assert CASE_SEVERITY == {"critical": "blocker", "high": "major", "medium": "minor", "low": "note", "info": "note"}
    assert DEPLOY_SEVERITY == {"critical": "blocker", "high": "major", "medium": "minor", "low": "note"}
    assert AUDIT_RULE_SEVERITY == {"a11y-blocker": "blocker", "a11y": "major", "usability-blocking": "blocker",
                                   "usability": "minor", "note": "note"}
    assert AUDIT_PROFILE_DEFAULT == "ui-audit"
    assert set(schemas.SEVERITY_ORDER) == set(SEVERITY_RANK)
    # the design's untouchables, re-asserted here so this PR cannot drift them: gates, forbidden tools, approval bridge
    import pathlib
    root = pathlib.Path(toolsmod.__file__).resolve().parents[2]
    assert "QA_EXTERNAL_TARGET_ACCEPTANCE_MET = True" in (root / "agentic_os/products/gates.py").read_text()
    assert gates.QA_EXTERNAL_TARGET_ACCEPTANCE_MET is True