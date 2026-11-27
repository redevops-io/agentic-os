"""Financial data needs the financial-data grant, on every way to read it.

Every money figure in the fake applications below is at least a million; nothing else here is that large (times are ten
digits). So "no number of seven to nine digits anywhere in what this caller was sent" is a test of every total, table,
chart, trend, drill-down and explanation at once, including ones added later."""
from __future__ import annotations

import json
import re

import pytest

pytest.importorskip("fastapi")

from agentic_os import financial_access as FA  # noqa: E402
from agentic_os.agent_gateway import agent_tokens as AT  # noqa: E402
from agentic_os.dashboards.config import parse_config  # noqa: E402
from agentic_os.dashboards.service import DEFINITIONS, parse_filters  # noqa: E402
from tests.test_dashboards import ACME, ADMIN, T0, _app, _Erp  # noqa: E402
from tests.test_economics import Erp as EconomicsErp, base_rows, recon_rows  # noqa: E402

SCALE = 10_000
MONEY_COLUMNS = {"grand_total", "outstanding_amount", "net_total", "price_list_rate", "rate", "net_amount", "incoming_rate", "base_costing_amount",
                 "paid_amount", "allocated_amount", "deposit", "withdrawal"}
BASE = "/api/agent-gateway/capabilities"


class RichErp(_Erp):
    def get_list(self, doctype, fields, filters=None, limit=0):
        return [{k: (v * SCALE if k in MONEY_COLUMNS else v) for k, v in r.items()} for r in super().get_list(doctype, fields, filters, limit)]


def rich_books():
    rows = {**base_rows(), **recon_rows(3380)}
    return EconomicsErp({k: [{c: (v * SCALE if c in MONEY_COLUMNS and isinstance(v, (int, float)) else v) for c, v in r.items()} for r in table]
                         for k, table in rows.items()})


class RichCrm:
    base_url, api_key = "http://crm.test", "k"

    def connected(self):
        return True

    def opportunities(self, limit=60):
        def deal(i, stage, amount, updated):
            return {"id": f"o{i}", "name": f"Deal {i}", "stage": stage, "amount": {"amountMicros": amount * 1_000_000, "currencyCode": "USD"}, "companyId": "c1",
                    "createdAt": "2026-11-10T00:00:00Z", "updatedAt": updated}
        return [deal(1, "PROPOSAL", 7_654_321, "2026-10-01T00:00:00Z"), deal(2, "CUSTOMER", 5_432_109, "2026-11-15T00:00:00Z"),
                deal(3, "LOST", 4_321_098, "2026-11-12T00:00:00Z")]

    def companies(self, limit=60):
        return [{"id": "c1", "name": "Acme", "createdAt": "2026-11-02T00:00:00Z"}]


class RichBilling:
    base_url = "http://billing.test"

    def connected(self):
        return True

    def _get(self, path, params=None):
        if path.endswith("subscriptions"):
            return {"subscriptions": []}
        return {"invoices": [{"lago_id": "i1", "number": "L-1", "payment_status": "failed", "total_amount_cents": 876_543_200, "currency": "USD",
                              "issuing_date": "2026-11-10"}], "meta": {"total_pages": 1}}

    def subscriptions(self, status="active", max_pages=10):
        return [{"external_id": "s1", "name": "Plan", "status": "active", "plan_code": "pro", "started_at": "2026-11-01T00:00:00Z"}]


class RichBi:
    base_url = "http://bi.test"

    def _client(self):
        import contextlib
        return contextlib.nullcontext(object())

    def _req(self, c, method, path, json=None):
        class R:
            status_code = 200

            def json(self):
                return {"data": {"cols": [{"name": "month"}, {"name": "revenue"}], "rows": [["2026-10", 3_210_987], ["2026-11", 2_109_876]]}}
        return R()


class Runtime:
    class repo:  # noqa: N801
        @staticmethod
        def timeline(mid):
            return [{"type": "PlanCreated", "ts": T0 - 590, "payload": {"plan_id": "p1", "signature": "a->b", "projection": {"expected_usd": 6_000_001.0, "within_budget": True}}},
                    {"type": "NodeDispatched", "ts": T0 - 500, "payload": {"node_id": "n1", "capability": "crm.read", "cost_usd": 6_543_210.0, "tokens": 1200}}]

    def missions(self):
        return [{"id": "m1", "state": "succeeded", "template": "invoice_recovery", "awaiting_human": False}]


SOURCES = ("erpnext", "twenty", "lago", "metabase", "mission_plane")


def connect(svc, *, synthetic=False):
    marked = {"data": "synthetic"} if synthetic else {}
    svc.config.sources.update({s: dict(marked) for s in SOURCES})
    svc.config.sources["metabase"]["cards"] = {"revenue_by_month": 7}
    svc.ctx.clients.update({"erpnext": RichErp(), "economics": rich_books(), "twenty": RichCrm(), "lago": RichBilling(), "metabase": RichBi()})
    svc.provider.mission_runtime = Runtime()
    svc.cache.invalidate()


LARGE = re.compile(r"(?<![0-9a-fA-F.])(?:\d{7,9}|\d{1,3}(?:,\d{3}){2})(?![0-9a-fA-F])")


def money_in(value, path="$"):
    """Where an amount of money shows up in something a caller was sent."""
    if isinstance(value, bool) or value is None:
        return []
    if isinstance(value, (int, float)):
        return [(path, value)] if 1_000_000 <= abs(value) < 1_000_000_000 else []
    if isinstance(value, str):
        return [(path, m.group(0)) for m in LARGE.finditer(value)]
    if isinstance(value, dict):
        return [f for k, v in value.items() for f in money_in(v, f"{path}.{k}")]
    if isinstance(value, (list, tuple)):
        return [f for i, v in enumerate(value) for f in money_in(v, f"{path}[{i}]")]
    return []


def read_everything(client, svc, headers):
    """The catalog, every dashboard (a longer range too), every drill-down and every explanation, as this caller."""
    seen = [client.get("/api/dashboards", headers=headers).json()]
    for df in DEFINITIONS:
        for params in ({}, {"days": 365}):
            got = client.get(f"/api/dashboards/{df.id}", params=params, headers=headers)
            seen.append(got.json())
            if got.status_code != 200:
                continue
            body = got.json()
            keys = {m["drill"] for p in body["panels"] for m in p["metrics"]} | {i.get("drill") for p in body["panels"] for i in p["items"]} \
                | {r.get("drill") for p in body["panels"] for r in p["rows"]}
            for key in sorted(k for k in keys if k):
                drill = client.get(f"/api/dashboards/{df.id}/drill", params={"key": key, **params}, headers=headers)
                assert drill.status_code == 200, (df.id, key, drill.text)
                seen.append(drill.json())
    return seen


def explanations(svc, principal):
    acc, out = svc.access(principal), []
    for df in DEFINITIONS:
        out.append(svc.explain(f"dashboard:{df.id}", acc))
        try:
            d = svc.build(df.id, parse_filters({}, now=svc.clock()), acc)
        except PermissionError:
            continue
        out += [svc.explain(f"dashboard:{df.id}#{key}", acc) for key in d.drills]
    return out


@pytest.fixture
def tenants(monkeypatch):
    def put(limits):
        monkeypatch.setenv("AGENTIC_OS_TENANT_LIMITS", json.dumps(limits))
    return put


NO_MONEY = {"acme": {"policy_refs": ["dashboards:*", "project:*"]}}
ALL_MONEY = {"acme": {"policy_refs": ["dashboards:*", "project:*", "egress:financial"]}}


def test_the_operator_sees_the_money_so_the_search_below_can_find_it(monkeypatch, tmp_path):
    c, svc, _ = _app(monkeypatch, tmp_path)
    connect(svc)
    found = money_in(read_everything(c, svc, {"Authorization": f"Bearer {ADMIN}"})) + money_in(explanations(svc, "admin"))
    for amount in (7_654_321, 8_765_432, 3_210_987, 6_543_210, 6_000_001, 10_000_000):
        assert any(float(str(v).replace(",", "")) == amount for _, v in found), amount


@pytest.mark.parametrize("who", ["anonymous", "tenant"])
def test_without_the_grant_no_dashboard_drill_trend_or_explanation_carries_money(monkeypatch, tmp_path, tenants, who):
    c, svc, _ = _app(monkeypatch, tmp_path)
    tenants(NO_MONEY)                                     # after _app, which starts from a clean environment
    connect(svc)
    # first someone who may see it reads everything: that fills the build cache and records the metric history
    read_everything(c, svc, {"Authorization": f"Bearer {ADMIN}"})
    headers, principal = ({}, "") if who == "anonymous" else ({"Authorization": f"Bearer {ACME}"}, "requester:acme")
    seen = read_everything(c, svc, headers)
    assert c.get("/api/dashboards/revenue", headers=headers).status_code == 200      # this caller does read dashboards
    assert money_in(seen) == []
    assert money_in(explanations(svc, principal)) == []
    # and it is said, not hidden: the refusal names the grant, and a withheld figure never reads as zero or "not recorded"
    catalog = seen[0]
    assert {"finance", "economics", "procurement"} <= {d["id"] for d in catalog["needs_financial_grant"]}
    refused = c.get("/api/dashboards/finance", headers=headers)
    assert refused.status_code == 403 and refused.json()["detail"]["error"] == "missing_grant" and "egress:financial" in refused.json()["detail"]["detail"]
    revenue = c.get("/api/dashboards/revenue", headers=headers).json()
    metrics = {m["id"]: m for p in revenue["panels"] for m in p["metrics"]}
    assert (metrics["pipeline"]["value"], metrics["pipeline"]["unknown"], metrics["won"]["value"], metrics["won"]["unknown"]) == (None, "unauthorized", None, "unauthorized")
    assert metrics["pipeline"]["trend"] == [] and metrics["open"]["value"] == 1 and revenue["withheld"]["financial"] and "egress:financial" in revenue["withheld"]["detail"]
    costs = c.get("/api/dashboards/ai_costs", headers=headers).json()
    spend = {m["id"]: m for p in costs["panels"] for m in p["metrics"]}
    assert spend["cost"]["unknown"] == "unauthorized" and spend["steps"]["value"] == 1 and spend["tokens"]["value"] == 1200
    assert "not recorded" not in costs["summary"] and "carry no spend" not in spend["cost"]["note"]


def test_a_tenant_granted_one_sources_money_sees_that_money_only(monkeypatch, tmp_path, tenants):
    c, svc, _ = _app(monkeypatch, tmp_path)
    tenants({"acme": {"policy_refs": ["dashboards:*", "project:*", "egress:financial:twenty"]}})
    connect(svc)
    found = {float(str(v).replace(",", "")) for _, v in money_in(read_everything(c, svc, {"Authorization": f"Bearer {ACME}"}))}
    assert 7_654_321 in found and not found & {8_765_432, 3_210_987, 6_543_210, 10_000_000}


def agent_reads(c, who):
    listed = c.post(f"{BASE}/dashboards.list/invoke", headers=who, json={}).json()
    out = [listed]
    for df in DEFINITIONS:
        out.append(c.post(f"{BASE}/dashboards.get/invoke", headers=who, json={"dashboard_id": df.id}).json())
    return out


def agent_world(monkeypatch, tmp_path, tenants, limits, *, synthetic=False):
    agents = str(tmp_path / "agents.json")
    monkeypatch.setenv("AGENT_GATEWAY_AGENTS_FILE", agents)
    for k in ("AGENT_GATEWAY_TOKEN", "AGENT_GATEWAY_AUDIT_PATH", "AGENT_GATEWAY_MCP"):
        monkeypatch.delenv(k, raising=False)
    import agentic_os.projects_api as api
    real = api.create_app

    def create(provider):
        tenants(limits)
        return real(provider)
    monkeypatch.setattr("tests.test_dashboards.create_app", create)
    c, svc, _ = _app(monkeypatch, tmp_path)
    connect(svc, synthetic=synthetic)
    count = iter(range(100))

    def agent(grants):
        return {"Authorization": "Bearer " + AT.issue(agents, f"a{next(count)}", grants, minutes=5, tenant="acme")}
    return c, svc, agent


@pytest.mark.parametrize("agent_grants,tenant_limits", [
    (["dashboards.read", "egress:financial"], NO_MONEY),             # the agent holds it, its tenant does not
    (["dashboards.read"], ALL_MONEY),                                # the tenant holds it, the agent was not given it
    (["dashboards.read", "demo:financial"], ALL_MONEY),              # a demonstration grant over real (undeclared) sources
    (["dashboards.read", "egress:financial"], {"acme": {"policy_refs": ["dashboards:*", "project:*", "demo:financial"]}}),
])
def test_an_agent_gets_money_only_when_it_and_its_tenant_both_hold_the_grant(monkeypatch, tmp_path, tenants, agent_grants, tenant_limits):
    c, svc, agent = agent_world(monkeypatch, tmp_path, tenants, tenant_limits)
    read_everything(c, svc, {"Authorization": f"Bearer {ADMIN}"})
    seen = agent_reads(c, agent(agent_grants))
    assert money_in(seen) == []
    assert seen[0]["status"] == "ok" and {"finance", "economics"} <= {d["id"] for d in seen[0]["output"]["needs_financial_grant"]}
    finance = next(r for df, r in zip(DEFINITIONS, seen[1:]) if df.id == "finance")
    assert finance["output"]["error"].startswith("not_authorized (missing_grant)") and "egress:financial" in finance["output"]["error"]


def test_with_both_grants_the_agent_reads_the_money(monkeypatch, tmp_path, tenants):
    c, svc, agent = agent_world(monkeypatch, tmp_path, tenants, ALL_MONEY)
    assert money_in(agent_reads(c, agent(["dashboards.read", "egress:financial"])))


# ── the public demonstration ─────────────────────────────────────────────────────────────────────────────────────
def test_the_demonstration_grant_reaches_declared_synthetic_sources_and_nothing_else(monkeypatch, tmp_path, tenants):
    limits = {"acme": {"policy_refs": ["dashboards:*", "project:*", "demo:financial"]}}
    c, svc, agent = agent_world(monkeypatch, tmp_path, tenants, limits, synthetic=True)
    shown = money_in(agent_reads(c, agent(["dashboards.read", "demo:financial"])))
    assert {float(str(v).replace(",", "")) for _, v in shown} & {10_000_000, 7_654_321}
    del svc.config.sources["erpnext"]["data"]                       # one source stops being declared synthetic
    svc.cache.invalidate()
    after = agent_reads(c, agent(["dashboards.read", "demo:financial"]))
    finance_after = next(r for df, r in zip(DEFINITIONS, after[1:]) if df.id == "economics")
    assert finance_after["output"]["error"].startswith("not_authorized (missing_grant)")


ACCESS = """
access:
  anonymous: {{projects: "*", sources: "*", financial: {money}}}
"""


def test_anonymous_visitors_get_synthetic_money_only_through_the_demo_grant(monkeypatch, tmp_path):
    c, svc, _ = _app(monkeypatch, tmp_path, access=ACCESS.format(money="demo"))
    assert svc.config.errors == [] and svc.config.anonymous.financial == ()          # nothing is declared synthetic: nothing is covered
    from tests.test_dashboards import CONFIG
    declared = CONFIG.format(data=tmp_path / "d2", access=ACCESS.format(money="demo")) + "sources:\n  erpnext: {data: synthetic}\n  twenty: {}\n"
    cfg = parse_config(__import__("yaml").safe_load(declared))
    assert cfg.errors == [] and cfg.anonymous.financial == ("erpnext",)


@pytest.mark.parametrize("money", ['"*"', "[erpnext]"])
def test_real_financial_data_cannot_be_given_to_anonymous_callers(tmp_path, money):
    from tests.test_dashboards import CONFIG
    cfg = parse_config(__import__("yaml").safe_load(CONFIG.format(data=tmp_path, access=ACCESS.format(money=money))))
    assert any("anonymous caller may only be given `demo`" in e for e in cfg.errors)
    assert cfg.anonymous.sources == () and cfg.anonymous.financial == ()          # a broken access section grants nothing


def test_only_synthetic_can_be_declared_about_a_source():
    cfg = parse_config({"schema": "aos.dashboards/v1", "sources": {"erpnext": {"data": "public"}}})
    assert any("may only be declared" in e for e in cfg.errors) and cfg.synthetic_sources() == ()


def test_the_grant_words():
    assert FA.covered(["egress:financial"]) is None
    assert FA.covered([]) == frozenset() and FA.covered(["dashboards:*", "financial", "egress:*"]) == frozenset()
    assert FA.covers(FA.covered(["egress:financial:erpnext"]), "economics") and not FA.covers(FA.covered(["egress:financial:erpnext"]), "lago")
    assert FA.covers(FA.covered(["egress:financial:mission_plane"]), "ai_usage")
    assert FA.covered(["demo:financial"]) == frozenset() and FA.covered(["demo:financial"], ["lago"]) == frozenset({"lago"})
    assert FA.both(None, frozenset({"lago"})) == frozenset({"lago"}) and FA.both(frozenset({"erpnext"}), frozenset({"lago"})) == frozenset()


def test_every_source_that_holds_money_is_classified():
    """A new money field in a collector must be classified here, or it would reach callers without the grant."""
    import inspect

    from agentic_os.dashboards import sources as S
    words = re.compile(r'"(amount|outstanding|grand_total|price|cost|cost_usd|expected_usd|total_amount_cents|revenue|salary|balance)"\s*:')
    for sid, fn in S.COLLECTORS.items():
        for field_name in words.findall(inspect.getsource(fn)):
            assert sid in FA.WHOLE or field_name in FA.FIELDS.get(sid, ()), f"{sid} records carry {field_name!r}: classify it in financial_access"
