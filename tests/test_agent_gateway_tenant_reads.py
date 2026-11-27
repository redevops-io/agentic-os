"""On a deployment with tenants an external agent reads only what its token's tenant is granted, dashboards included;
``dashboards.read`` is a grant that has to be named. Through the mounted gateway, over the fake ERP and support desk."""
import json

import pytest

from agentic_os.agent_gateway import agent_tokens as AT
from agentic_os.agent_gateway.capabilities import AGENT_MAX_ROWS, bound_rows, mask_emails
from agentic_os.projects_api import SampleProjectionProvider
from tests.test_dashboards import T0, _app, _Chat, _Erp

LIMITS = {
    "erp-only": {"policy_refs": ["dashboards:erpnext", "project:*", "egress:financial"]},
    "desk-only": {"policy_refs": ["dashboards:chatwoot", "project:*"]},
    "storefront-only": {"policy_refs": ["dashboards:*", "project:storefront", "egress:financial"]},
    "everything": {"policy_refs": ["dashboards:*", "project:*", "egress:financial"]},
    "lapsed": {"policy_refs": ["dashboards:*", "project:*"], "expires_at": T0 - 10_000_000},
}
ALL_GRANTS = sorted(AT.ALLOWED_GRANTS)
BASE = "/api/agent-gateway/capabilities"


@pytest.fixture
def deployment(monkeypatch, tmp_path):
    for k in ("AGENT_GATEWAY_TOKEN", "AGENT_GATEWAY_AUDIT_PATH", "AGENT_GATEWAY_MCP"):
        monkeypatch.delenv(k, raising=False)
    agents = str(tmp_path / "agents.json")

    def make(limits=LIMITS, shared=""):
        monkeypatch.setenv("AGENT_GATEWAY_AGENTS_FILE", agents)
        if shared:
            monkeypatch.setenv("AGENT_GATEWAY_TOKEN", shared)
        import agentic_os.projects_api as api
        real = api.create_app

        def create(provider):                                     # the limits must be in place when the app is built and when it is read
            if limits is not None:
                monkeypatch.setenv("AGENTIC_OS_TENANT_LIMITS", limits if isinstance(limits, str) else json.dumps(limits))
            return real(provider)

        monkeypatch.setattr("tests.test_dashboards.create_app", create)
        client, svc, _ = _app(monkeypatch, tmp_path)
        svc.config.sources.update({"erpnext": {"project": "billing"}, "chatwoot": {}})
        svc.ctx.clients.update({"erpnext": _Erp(), "chatwoot": _Chat()})
        svc.cache.invalidate()
        return client

    made = {"n": 0}

    def agent(tenant, grants=ALL_GRANTS):
        made["n"] += 1
        return {"Authorization": "Bearer " + AT.issue(agents, f"a{made['n']}", list(grants), days=1, tenant=tenant)}

    return make, agent


def offered(client, who):
    body = client.get(BASE, headers=who).json()
    return sorted(c["name"] for c in (body.get("capabilities", body) if isinstance(body, dict) else body))


def invoke(client, who, name, **arguments):
    return client.post(f"{BASE}/{name}/invoke", headers=who, json=arguments).json()          # the body IS the arguments


def figure(out, metric):
    return next(m["value"] for p in out["output"]["panels"] for m in p["metrics"] if m["id"] == metric)


def ids(out):
    return {d["id"] for d in out["output"]["dashboards"]}


def test_dashboards_are_not_part_of_the_default_reads(deployment):
    make, agent = deployment
    client = make()
    plain = agent("everything", sorted(AT.READ_GRANTS))
    assert not [n for n in offered(client, plain) if n.startswith("dashboards.")]
    assert invoke(client, plain, "dashboards.get", dashboard_id="finance")["status"] == "denied"
    only = agent("everything", ["dashboards.read"])
    assert offered(client, only) == ["dashboards.get", "dashboards.list"]


def test_one_tenants_agent_cannot_count_another_tenants_records(deployment):
    make, agent = deployment
    client = make()
    erp, desk = agent("erp-only"), agent("desk-only")
    erp_ids, desk_ids = ids(invoke(client, erp, "dashboards.list")), ids(invoke(client, desk, "dashboards.list"))
    assert "finance" in erp_ids and "support" not in erp_ids
    assert "support" in desk_ids and "finance" not in desk_ids
    assert figure(invoke(client, erp, "dashboards.get", dashboard_id="finance"), "receivables") == 1500.0
    refused = invoke(client, desk, "dashboards.get", dashboard_id="finance")["output"]
    assert refused == {"error": "not permitted"}
    assert invoke(client, erp, "dashboards.get", dashboard_id="support")["output"] == {"error": "not permitted"}


def test_a_tenant_limited_to_one_project_counts_no_other_projects_records(deployment):
    make, agent = deployment
    client = make()
    narrow = agent("storefront-only")                          # the ERP is attributed to the billing project
    assert figure(invoke(client, narrow, "dashboards.get", dashboard_id="finance"), "receivables") != 1500.0
    assert invoke(client, narrow, "dashboards.get", dashboard_id="finance", project="billing")["output"] == {"error": "not permitted"}


@pytest.mark.parametrize("tenant", ["nobody", "lapsed", ""])
def test_a_tenant_with_no_current_grant_reads_nothing(deployment, tenant):
    make, agent = deployment
    client = make()
    who = agent(tenant or "default")
    assert invoke(client, who, "dashboards.list")["output"]["dashboards"] == []
    assert invoke(client, who, "dashboards.get", dashboard_id="finance")["output"] == {"error": "not permitted"}
    assert invoke(client, who, "projects.list")["output"] == []
    assert invoke(client, who, "missions.list")["output"] == []
    assert invoke(client, who, "sources.search", query="a")["output"] == []
    assert invoke(client, who, "projects.get_status", project_id="")["status"] == "error"
    assert invoke(client, who, "missions.get", mission_id="m-1")["status"] == "error"


def test_limits_that_cannot_be_read_open_nothing(deployment):
    make, agent = deployment
    client = make(limits="{broken")
    who = agent("everything")
    assert invoke(client, who, "projects.list")["output"] == []
    assert invoke(client, who, "dashboards.get", dashboard_id="finance")["output"] == {"error": "not permitted"}


def test_a_granted_tenant_reads_as_before_and_a_deployment_without_tenants_is_unchanged(deployment):
    make, agent = deployment
    client = make()
    full = agent("everything")
    listed = invoke(client, full, "projects.list")["output"]
    assert [p["id"] for p in listed] == [p["id"] for p in SampleProjectionProvider().projects()]
    assert invoke(client, full, "projects.get_status", project_id=listed[0]["id"])["status"] == "ok"
    assert invoke(client, full, "missions.list")["output"]


def test_without_tenant_limits_reads_are_not_scoped_but_dashboards_still_need_a_grant(deployment):
    make, agent = deployment
    client = make(limits=None)
    who = agent("default")
    assert invoke(client, who, "projects.list")["output"] and invoke(client, who, "missions.list")["output"]
    assert invoke(client, who, "dashboards.get", dashboard_id="finance")["output"] == {"error": "not permitted"}


def test_a_project_a_tenant_may_not_see_looks_like_one_that_does_not_exist(deployment):
    make, agent = deployment
    own = SampleProjectionProvider().projects()[0]["id"]
    client = make(limits={"other": {"policy_refs": ["project:somewhere-else"]}, "mine": {"policy_refs": [f"project:{own}"]}})
    outsider = agent("other")
    a, b = invoke(client, outsider, "projects.get_status", project_id=own), invoke(client, outsider, "projects.get_status", project_id="no-such")
    assert (a["status"], a.get("error")) == (b["status"], b.get("error")) == ("error", "no such project")
    assert invoke(client, agent("mine"), "projects.get_status", project_id=own)["status"] == "ok"


def test_the_shared_bearer_never_reads_dashboards(deployment):
    make, _agent = deployment
    client = make(shared="shared-bearer-value")
    who = {"Authorization": "Bearer shared-bearer-value"}
    assert not [n for n in offered(client, who) if n.startswith("dashboards.")]
    assert invoke(client, who, "projects.list")["output"] == []     # with tenants, it is the tenant "demo": no entry, nothing


def test_a_long_table_is_cut_and_says_so_and_addresses_are_masked_but_digests_are_not():
    sha = "c0229c6490b1d0a5a7c2d4e8f1a3b5c7d9e0f2a4"
    dash = {"panels": [{"id": "open", "rows": [{"cells": {"who": f"p{i}@example.com", "commit": sha, "n": i}} for i in range(25)]},
                       {"id": "stats", "metrics": [{"id": "x", "value": 25}]}]}
    out = mask_emails(bound_rows(dash))
    big = out["panels"][0]
    assert len(big["rows"]) == AGENT_MAX_ROWS and big["rows_total"] == 25 and "15 more" in big["rows_note"]
    assert "@example.com" not in json.dumps(out) and big["rows"][0]["cells"]["commit"] == sha and out["panels"][1]["metrics"][0]["value"] == 25


# ── the same under the production profile, where every call also needs a stored authority record ───────────────────────
def test_in_production_the_authority_record_covers_the_dashboard_reads_that_were_named(deployment, monkeypatch, tmp_path):
    db, agents = str(tmp_path / "authority.sqlite"), str(tmp_path / "agents.json")
    for k, v in {"AGENTIC_OS_PROFILE": "production", "AGENTIC_OS_AUTHORITY_DB": db, "AGENTIC_OS_APPROVAL_SECRET": "s" * 48,
                 "AGENTIC_OS_REPLAY_DB": str(tmp_path / "replay.sqlite"), "AGENT_GATEWAY_AGENTS_FILE": agents}.items():
        monkeypatch.setenv(k, v)
    # _app clears the profile before it builds the app, so it is put back as the app is created
    import agentic_os.projects_api as api
    real = api.create_app

    def create(provider):
        monkeypatch.setenv("AGENTIC_OS_PROFILE", "production")
        monkeypatch.setenv("AGENTIC_OS_TENANT_LIMITS", json.dumps(LIMITS))
        return real(provider)

    monkeypatch.setattr("tests.test_dashboards.create_app", create)
    client, svc, _ = _app(monkeypatch, tmp_path)
    monkeypatch.setattr("tests.test_dashboards.create_app", real)
    svc.config.sources.update({"erpnext": {"project": "billing"}})
    svc.ctx.clients.update({"erpnext": _Erp()})
    svc.cache.invalidate()

    def issue(ident, grants, tenant, **kw):
        return {"Authorization": "Bearer " + AT.issue(agents, ident, grants, minutes=5, tenant=tenant, **kw)}

    named = issue("named", ["dashboards.read", "projects.read", "egress:financial"], "erp-only", authority_db=db)
    assert figure(invoke(client, named, "dashboards.get", dashboard_id="finance"), "receivables") == 1500.0
    assert invoke(client, named, "dashboards.list")["status"] == "ok" and invoke(client, named, "projects.list")["status"] == "ok"
    plain = issue("plain", ["projects.read"], "erp-only", authority_db=db)
    assert invoke(client, plain, "dashboards.get", dashboard_id="finance")["status"] == "denied"
    no_record = issue("unrecorded", ["dashboards.read"], "erp-only")
    assert invoke(client, no_record, "dashboards.get", dashboard_id="finance")["status"] == "denied"
