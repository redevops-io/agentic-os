"""Tenant callers get grants and a spend ceiling from server config; the registry keeps one owner per name."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.service import build_pilot_runtime, mount
from agentic_os.mission.types import CapabilityManifest, CapabilitySpec

ADMIN, TOK = "admin-secret", "tok-acme"
H = lambda t: {"Authorization": f"Bearer {t}"}  # noqa: E731
GRANTS = ["billing:write", "support:write", "books:write", "compliance:write"]


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", ADMIN)
    monkeypatch.setenv("AGENTIC_OS_TENANT_TOKENS", f"acme={TOK}")
    monkeypatch.setenv("AGENTIC_OS_TENANT_LIMITS", json.dumps({"acme": {"policy_refs": GRANTS, "budget_usd": 20}}))
    monkeypatch.delenv("AGENTIC_OS_APPROVER_TOKENS", raising=False)
    app = FastAPI()
    rt = mount(app, build_pilot_runtime())
    return TestClient(app), rt


def _create(c, token, **body):
    return c.post("/missions", headers=H(token), json={"goal": "Onboard a new customer", "template": "onboarding", **body})


def test_tenant_wildcard_grants_rejected(client):
    c, _ = client
    assert _create(c, TOK, policy_refs=["*"]).status_code == 422


def test_tenant_grants_come_from_config(client):
    c, rt = client
    r = _create(c, TOK)
    assert r.status_code == 200
    assert sorted(rt._missions[r.json()["id"]].policy_refs) == sorted(GRANTS)
    r = _create(c, TOK, policy_refs=["billing:write", "other:write"])
    assert rt._missions[r.json()["id"]].policy_refs == ["billing:write"]


def test_tenant_without_config_has_no_grants(client, monkeypatch):
    c, rt = client
    monkeypatch.setenv("AGENTIC_OS_TENANT_LIMITS", json.dumps({"other": {"policy_refs": GRANTS}}))
    r = _create(c, TOK)
    assert rt._missions[r.json()["id"]].policy_refs == []


def test_budget_is_clamped_and_validated(client):
    c, rt = client
    assert rt._missions[_create(c, TOK, budget_usd=1000).json()["id"]].budget.usd == 20.0
    assert rt._missions[_create(c, TOK, budget_usd=3).json()["id"]].budget.usd == 3.0
    assert rt._missions[_create(c, TOK).json()["id"]].budget.usd == 20.0
    assert _create(c, TOK, budget_usd=-1).status_code == 422
    raw = '{"goal":"x","template":"onboarding","budget_usd":Infinity}'
    r = c.post("/missions", headers={**H(ADMIN), "content-type": "application/json"}, content=raw)
    assert r.status_code == 422
    assert rt._missions[_create(c, ADMIN, budget_usd=0).json()["id"]].budget.usd == 0.0


def _manifest(op, **kw):
    spec = dict(name="billing.refund", operator="", side_effecting=True, approval_required=True,
                permissions=["billing:write"], irreversible=True, irreversible_note="n")
    spec.update(kw)
    return CapabilityManifest(operator=op, capabilities=[CapabilitySpec(**spec)])


def test_other_operator_cannot_replace_a_capability():
    reg = CapabilityRegistry()
    reg.register(_manifest("billing"))
    with pytest.raises(ValueError):
        reg.register(_manifest("other", approval_required=False, permissions=[]))
    assert reg.get("billing.refund").approval_required is True


def test_same_operator_cannot_weaken_but_can_strengthen():
    reg = CapabilityRegistry()
    reg.register(_manifest("billing"))
    for weaker in ({"approval_required": False}, {"side_effecting": False}, {"permissions": []},
                   {"irreversible": False, "irreversible_note": ""}):
        with pytest.raises(ValueError):
            reg.register(_manifest("billing", **weaker))
    reg.register(_manifest("billing"))                                    # identical re-registration
    reg.register(_manifest("billing", permissions=["billing:write", "audit:write"]))
    assert reg.get("billing.refund").permissions == ["billing:write", "audit:write"]


def test_remote_manifest_cannot_name_another_operator():
    from agentic_os.mission.production import discover
    docs = {"http://a": {"operator": "a", "capabilities": [{"name": "a.x", "operator": "b"}]}}
    with pytest.raises(ValueError):
        discover({"a": "http://a"}, fetch=lambda u: docs[u.rsplit("/capabilities", 1)[0]])


def test_more_weakening_declarations_are_refused():
    from agentic_os.mission.types import NodeCost
    base = dict(cost=NodeCost(usd=1.0), required_authority=["a"], network=["x.example"], secrets=["s"],
                inputs={"a": "str"}, outputs={"b": "str"}, undo="billing.reverse", concurrency_mode="exclusive")
    reg = CapabilityRegistry()
    reg.register(_manifest("billing", **base))
    for change in ({"cost": NodeCost(usd=0.0)}, {"required_authority": []}, {"network": []}, {"secrets": []},
                   {"inputs": {}}, {"outputs": {}}, {"undo": None}, {"concurrency_mode": ""},
                   {"deterministic": False}):
        with pytest.raises(ValueError):
            reg.register(_manifest("billing", **{**base, **change}))


def test_duplicate_names_in_one_manifest_are_refused():
    spec = dict(name="x.a", operator="")
    m = CapabilityManifest(operator="x", capabilities=[CapabilitySpec(**spec), CapabilitySpec(**spec)])
    with pytest.raises(ValueError):
        CapabilityRegistry().register(m)


def test_discovery_binds_operator_name_to_configuration():
    from agentic_os.mission.production import discover
    docs = {"http://a": {"operator": "a", "capabilities": [{"name": "a.x"}]},
            "http://b": {"operator": "a", "capabilities": [{"name": "a.y"}]}}
    fetch = lambda u: docs[u.rsplit("/capabilities", 1)[0]]  # noqa: E731
    with pytest.raises(ValueError):
        discover({"a": "http://a", "b": "http://b"}, fetch=fetch)
    reg, resolve = discover({"a": "http://a"}, fetch=fetch)
    assert resolve == {"a": "http://a"}


def test_cockpit_does_not_post_wildcard_grants():
    from agentic_os.mission import api
    import inspect
    assert "policy_refs:['*']" not in inspect.getsource(api)


def test_security_fields_cannot_be_relaxed():
    base = dict(required_authority=["a", "b"], network=["x.example", "y.example"], secrets=["s"],
                data_classifications=["pii"], isolation_class="sandbox")
    reg = CapabilityRegistry()
    reg.register(_manifest("billing", **base))
    for change in ({"required_authority": ["a"]}, {"required_authority": []},
                   {"network": ["x.example", "z.example"]}, {"network": []},
                   {"secrets": []}, {"secrets": ["s", "t"]}, {"data_classifications": []},
                   {"isolation_class": "in_process"}, {"isolation_class": ""}):
        with pytest.raises(ValueError):
            reg.register(_manifest("billing", **{**base, **change}))
    reg.register(_manifest("billing", **{**base, "network": ["x.example"], "isolation_class": "strict",
                                         "required_authority": ["a", "b", "c"]}))
