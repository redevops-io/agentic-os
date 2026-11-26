"""The committed capability manifests are what the code says today. A new operation, route, tool or gateway capability that is
not in them fails here; regenerate with ``python -m agentic_os.app_kit.capability_inventory --write`` and review the diff."""
from __future__ import annotations

import hashlib
import json

import pytest

from agentic_os.app_kit import capability_inventory as inv


@pytest.fixture(scope="module")
def generated():
    return inv.generate()


def test_every_app_directory_has_a_manifest_and_none_is_stale(generated):
    assert set(generated) == set(inv.app_names(inv.repo_root()))
    assert inv.stale(generated) == [], "regenerate: python -m agentic_os.app_kit.capability_inventory --write"


def test_only_an_app_that_could_not_be_read_is_incomplete_and_it_says_why(generated):
    incomplete = {a: m["problems"] for a, m in generated.items() if m["status"] != "complete"}
    assert set(incomplete) == {"video-ad", "projects"}, incomplete
    assert "reel" in " ".join(incomplete["video-ad"])                       # it imports a package this repository does not contain
    assert incomplete["projects"] == ["the Projects web UI is built from another repository (redevops-projects): its calls are not inventoried"]


def test_every_route_has_a_gate_decision(generated):
    missing = {a: [f'{r["method"]} {r["path"]}' for r in m["routes"] if r["gate"] == "not_in_route_table"] for a, m in generated.items()}
    assert {a: v for a, v in missing.items() if v} == {}


def test_an_operation_is_reachable_only_if_the_gateway_registers_it(generated):
    registered = set(inv.gateway_capabilities())
    for manifest in generated.values():
        for op in manifest["operations"]:
            access = op["external_access"]
            assert access["gateway"] is (op["id"] in registered), op["id"]
            assert access["gateway"] or access["reason"]


def test_each_manifest_is_versioned_and_its_digest_is_of_its_content(generated):
    for manifest in inv.load_all().values():
        body = {k: v for k, v in manifest.items() if k != "digest"}
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
        assert manifest["schema"] == inv.SCHEMA
        assert manifest["digest"] == "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest(), manifest["app"]


def test_a_route_that_is_hidden_inside_an_included_router_is_still_seen():
    from fastapi import APIRouter, FastAPI
    router = APIRouter()
    router.add_api_route("/hidden", lambda: None, methods=["POST"], include_in_schema=False)
    app = FastAPI()
    app.include_router(router, prefix="/x")
    seen: set = set()
    inv.walk_routes(app.router.routes, seen)
    assert ("POST", "/x/hidden") in seen


def test_a_ui_call_is_matched_to_a_route_or_left_visibly_unmatched():
    routes = [("GET", "/api/items/{item}"), ("GET", "/api/v1/cases"), ("GET", "/health")]
    assert inv.match_ui_call("/api/items/", routes) == "/api/items/{item}"
    assert inv.match_ui_call("/cases", routes) == "/api/v1/cases"
    assert inv.match_ui_call("/rest/companies", routes) is None


def test_every_shape_of_front_end_call_the_apps_use_is_found(tmp_path):
    (tmp_path / "page.py").write_text("""HTML = "<script>fetch('api/agent',{method:'POST'}); fetch('/api/fleet')</script><form action='agent/run'>" """)
    (tmp_path / "ui.js").write_text("api('/cases'); fetch(`${base}/api/items/` + id); getJSON('v1/posture')")
    (tmp_path / "server.py").write_text("fetch('/rest/companies')   # server code in a file with no page: not a UI call")
    assert inv.ui_calls(tmp_path) == sorted(["/api/agent", "/api/fleet", "/agent/run", "/api/items/", "/cases", "/v1/posture"])


def test_the_inventory_sees_every_read_the_gateway_registers_dashboards_included():
    from agentic_os.agent_gateway.capabilities import READ_CAPABILITIES
    from agentic_os.app_kit.capability_inventory import gateway_capabilities
    from agentic_os.agent_gateway.product_capabilities import manifests as product_manifests
    from agentic_os.products.schemas import FORBIDDEN_TOOLS
    registered = set(gateway_capabilities())
    assert {"dashboards.list", "dashboards.get"} <= registered and {m.name for m in product_manifests()} <= registered
    assert {c.name for c in READ_CAPABILITIES} - registered == {"crm.lookup_account"}     # the mount gives it no CRM port
    assert not registered & FORBIDDEN_TOOLS


def test_a_candidate_lacks_nothing_and_every_other_unreachable_read_says_what_it_lacks():
    from agentic_os.app_kit.capability_inventory import gateway_blockers
    ready = {"external_access": {"gateway": False}, "action": "read", "typed": True, "http": [{"method": "GET", "path": "/x"}],
             "data_classifications": ["internal"], "side_effecting": False}
    assert gateway_blockers(ready) == []
    assert gateway_blockers({**ready, "typed": False, "http": [{"method": "POST", "path": "/x"}], "data_classifications": []}) == [
        "no typed inputs and outputs", "no GET route", "no data classification"]
    for never in ({"external_access": {"gateway": True}}, {"action": "write"}, {"side_effecting": True}, {"route_access": ["human_only"]}):
        assert gateway_blockers({**ready, **never}) is None
