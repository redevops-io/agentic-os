"""Two-sided public-demo contract tests (owner clarification 2026-11-08; audit §10).

LOCAL side (always, CI): run every contract app with Starlette's TestClient —
  * POSITIVE  anonymous documented demo pages + safe read-only demo APIs keep answering their pinned
    status + content marker with NO credentials: functionality;
  * NEGATIVE  anonymous privileged operations are refused (and refused WITHOUT state change), and the
    positive class is re-asserted in the same run so a blanket rule can never fake a green: containment.
  * STRUCTURAL no router-level auth dependency or auth middleware exists on any demo app.

LIVE side (only with DEMO_BASE_URL set — nightly workflow): probe the hosted surfaces so an edge-rule
change or a privatising deploy fails loudly. `live_only`/`live_status` cover documented deploy drift
(the /api/demo/scenarios 404 until the demo host is redeployed from main).

`pending_owner` entries pin TODAY's behaviour of unclassified surfaces (e.g. /admin): any change — in
either direction — fails until the owner classifies them.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = json.loads((ROOT / "audit" / "public_demo_contract.json").read_text())

_apps: dict = {}


def _client(name):
    if name not in _apps:
        spec = importlib.util.spec_from_file_location(f"demo_{name}", ROOT / CONTRACT["apps"][name])
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _apps[name] = TestClient(mod.app)
    return _apps[name]


def _get(client, path):
    return client.get(path, headers={})           # deliberately NO Authorization header


# ── POSITIVE: documented public journeys answer anonymously (functionality) ─────────────────────────

@pytest.mark.parametrize("ep", [e for e in CONTRACT["public_journeys"] if e.get("app")],
                         ids=lambda e: e["id"])
def test_public_journey_anonymous_local(ep):
    r = _get(_client(ep["app"]), ep["path"])
    assert r.status_code in ep["status"], (ep["id"], r.status_code, r.text[:120])
    if ep.get("marker"):
        assert ep["marker"] in r.text, (ep["id"], "content marker missing")
    assert "www-authenticate" not in {k.lower() for k in r.headers}, f"{ep['id']}: public page challenged"


# ── NEGATIVE: protected operations stay refused anonymously, WITHOUT state change (containment) ────

@pytest.mark.parametrize("ep", CONTRACT["protected_anonymous"], ids=lambda e: e["id"])
def test_protected_operation_refused_anonymous_local(ep):
    c = _client(ep["app"])
    if ep["method"] == "GET":
        r = c.get(ep["path"])
    elif ep["method"] == "DELETE":
        r = c.delete(ep["path"])
    else:
        r = c.post(ep["path"], json={})
    assert r.status_code in ep["deny"], (ep["id"], r.status_code, r.text[:120])
    assert not (200 <= r.status_code < 300), f"{ep['id']}: PRIVILEGED ROUTE SERVED ANONYMOUSLY"
    if ep.get("no_state_change"):
        after = c.get("/api/permissions/grants")
        assert after.status_code in ep["deny"], "list must stay refused; a created grant must not exist"
        # and the refusal must not be a blanket: the positive class is re-asserted HERE, same run
        assert _get(c, "/permissions").status_code == 200, "public page must still answer — blanket auth detected"


# ── STRUCTURAL: no blanket authentication on any demo app ──────────────────────────────────────────

@pytest.mark.parametrize("name", list(CONTRACT["apps"]))
def test_demo_app_has_no_blanket_auth(name):
    app = _client(name).app
    assert list(app.router.dependencies) == [], f"{name}: router-level dependency = blanket auth"
    for m in app.user_middleware:
        cls = getattr(m, "cls", None) or getattr(m, "middleware", None)
        assert "auth" not in (getattr(cls, "__name__", "") or "").lower(), \
            f"{name}: auth middleware {cls!r} covers the whole app — must be route-scoped"


def test_unclassified_surfaces_are_pinned_and_flagged():
    """pending-owner entries pin TODAY's behaviour so /admin-style surfaces cannot drift silently."""
    ids = {e["id"] for e in CONTRACT["pending_owner"]}
    assert {"admin-api", "admin-page"} <= ids
    for ep in CONTRACT["pending_owner"]:
        r = _client(ep["app"]).get(ep["path"])
        assert r.status_code in ep["pinned_status"], \
            f"{ep['id']} changed WITHOUT owner classification: {r.status_code} not in {ep['pinned_status']}"
        assert ep["marker"] in r.text


# ── LIVE: availability + response contract (nightly; skipped in ordinary CI) ───────────────────────

LIVE = os.environ.get("DEMO_BASE_URL", "").rstrip("/")


def _live_get(path):
    url = path if path.startswith("http") else LIVE + path
    req = urllib.request.Request(url, headers={"User-Agent": "agentic-os-demo-probe/1"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        # a pinned non-2xx journey (/unsubscribe answers 400 by design) is an ANSWER, not a transport
        # failure: the response object has to come back or the marker check can never see it
        try:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception:
            return e.code, ""


def _live_status(method, path):
    req = urllib.request.Request(LIVE + path, method=method, headers={"User-Agent": "agentic-os-demo-probe/1"},
                                 data=b"{}" if method == "POST" else None)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


@pytest.mark.skipif(not LIVE, reason="live demo probe runs in the nightly workflow (DEMO_BASE_URL)")
@pytest.mark.parametrize("ep", CONTRACT["public_journeys"], ids=lambda e: e["id"])
def test_public_journey_live(ep):
    status, body = _live_get(ep["path"])
    want = ep.get("live_status") or ep["status"]
    assert status in want, (ep["id"], status)
    # the marker is checked against the documented status; a widened live_status records known deploy
    # drift (an older build), where demanding the marker too would report drift twice
    if ep.get("marker") and status in ep["status"]:
        assert ep["marker"] in body, (ep["id"], "live marker missing")


LIVE_SAFE_METHODS = ("GET", "HEAD")
# The live surface is defined by this filter, not by a workflow's `-k` expression: what cannot be sent
# cannot be sent by accident. A live POST/PUT/PATCH/DELETE probe would execute the mutation it exists to
# detect if a guard had regressed. Refusal of those methods is proven offline
# (test_protected_operation_refused_anonymous_local), against the repo's own apps, with a state-change check.
LIVE_PROBES = [ep for ep in CONTRACT["protected_anonymous"] if ep["method"] in LIVE_SAFE_METHODS]


@pytest.mark.skipif(not LIVE, reason="live demo probe runs in the nightly workflow (DEMO_BASE_URL)")
@pytest.mark.parametrize("ep", LIVE_PROBES, ids=lambda e: e["id"])
def test_protected_refused_live(ep):
    status = _live_status(ep["method"], ep["path"])
    assert status >= 400, f"{ep['id']}: live privileged route served anonymously ({status})"


def test_no_mutating_probe_can_ever_be_sent_at_the_live_demo():
    """The negative test above is structurally incapable of writing."""
    assert LIVE_PROBES, "the anonymous read probes should not silently vanish"
    assert {e["method"] for e in LIVE_PROBES} <= set(LIVE_SAFE_METHODS)
    skipped = [e["id"] for e in CONTRACT["protected_anonymous"] if e["method"] not in LIVE_SAFE_METHODS]
    assert skipped, "the contract should still carry write routes, refused offline rather than live"


def test_the_live_probe_script_issues_only_get_and_head(monkeypatch):
    """Every request the nightly script sends is captured and asserted read-only — not just the code path
    this run happens to exercise: urllib.request.urlopen is replaced, so a mutating call would surface here
    as a recorded method rather than reaching a hosted demo."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "probe_ro", Path(__file__).resolve().parents[1] / "scripts" / "probe_public_demo.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sent = []

    class Resp:
        status = 404

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(req, timeout=None):
        sent.append(req.get_method())
        return Resp()

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    mod.main()
    assert sent, "the probe sent nothing — the capture is not working"
    assert set(sent) <= {"GET", "HEAD"}, f"the live probe issued mutating methods: {sorted(set(sent))}"


def test_a_widened_live_status_tolerates_a_missing_marker_but_a_pinned_status_never_does(monkeypatch, capsys):
    """The behaviour this fix exists for, in both directions.

    An older deployed build answers 404 where the contract pins 200; `live_status` records that known drift,
    and demanding the body marker from a 404 reports one condition twice and buries which surface drifted.
    A response that DOES match the pinned status is still checked strictly — including a designed non-2xx
    whose marker lives in the error body. The contract is narrowed to the one journey under test so the exit
    code can only be about that journey.
    """
    import importlib.util
    import urllib.request as ur
    spec = importlib.util.spec_from_file_location(
        "probe_marker", Path(__file__).resolve().parents[1] / "scripts" / "probe_public_demo.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    widened = [dict(ep) for ep in mod.CONTRACT["public_journeys"] if ep.get("live_status")]
    assert widened, "this test needs the widened entry it was written for (/api/demo/scenarios)"
    ep = widened[0]
    assert 200 in ep["status"] and 404 in ep["live_status"] and ep.get("marker")
    monkeypatch.setattr(mod, "CONTRACT", {"public_journeys": [ep], "pending_owner": [], "protected_anonymous": []})

    def run(status, body):
        class Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return body.encode()

        def fake(req, timeout=None):
            if status >= 400:
                raise ur.HTTPError(req.full_url, status, "x", {}, io.BytesIO(body.encode()))
            return Resp()

        monkeypatch.setattr(mod.urllib.request, "urlopen", fake)
        code = mod.main()
        return code, capsys.readouterr().out

    # 1. widened drift: an older build's 404 is tolerated, reported once, and NOT doubled into a marker failure
    code, out = run(404, "")
    assert code == 0, f"widened live drift must pass: {out}"
    assert "widened live drift" in out and "marker check skipped" in out, f"drift must stay visible: {out}"
    assert "VIOLATED" not in out

    # 2. a response that matches the pinned status is still checked strictly
    code, out = run(200, "no marker here")
    assert code == 1 and "live marker" in out, f"a pinned-status response with a missing marker must fail: {out}"
    assert run(200, ep["marker"])[0] == 0

    # 3. a designed non-2xx whose marker lives in the error body keeps its strict check (e.g. /unsubscribe 400)
    designed = [dict(e, status=[400], live_status=None) for e in mod.CONTRACT["public_journeys"] if e.get("marker")]
    assert designed
    monkeypatch.setattr(mod, "CONTRACT", {"public_journeys": designed, "pending_owner": [],
                                          "protected_anonymous": []})
    code, out = run(400, "no marker")
    assert code == 1 and "live marker" in out, f"a pinned 400 must still be marker-checked: {out}"
    assert run(400, designed[0]["marker"])[0] == 0


# ---- the contract file, the nightly probe and the job that runs it ------------------------------------

def _probe_module():
    p = ROOT / "scripts" / "probe_public_demo.py"
    spec = importlib.util.spec_from_file_location("probe_public_demo", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_contract_file_is_wellformed_and_points_at_real_apps():
    assert CONTRACT["version"] == 1 and CONTRACT["live_base"].startswith("https://")
    for name, rel in CONTRACT["apps"].items():
        assert (ROOT / rel).is_file(), f"contract app {name} points at a missing file: {rel}"
    seen = set()
    for group in ("public_journeys", "pending_owner", "protected_anonymous"):
        for ep in CONTRACT[group]:
            assert ep["id"] not in seen, f"duplicate contract id {ep['id']}"
            seen.add(ep["id"])
            assert ep["path"].startswith("/") or ep["path"].startswith("https://"), ep
            assert ep.get("status") or ep.get("pinned_status") or ep.get("deny"), ep
            if ep.get("app"):
                assert ep["app"] in CONTRACT["apps"], ep
    for ep in CONTRACT["pending_owner"]:
        assert len(ep["question"]) > 30, f"{ep['id']}: an unclassified surface needs a real question"
        assert ep["pinned_status"] and ep["marker"], f"{ep['id']}: pending-owner must pin today's behaviour"


def test_the_nightly_probe_reads_the_body_of_a_pinned_non_2xx_journey(monkeypatch):
    """/unsubscribe answers 400 BY DESIGN and its marker lives in that error body; a probe that discards
    error bodies reports a working public page as broken."""
    import io
    import urllib.error as ue
    mod = _probe_module()

    def fake_urlopen(req, timeout=None):
        raise ue.HTTPError("u", 400, "Bad Request", {}, io.BytesIO(b"unsubscribe link is invalid"))

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    assert mod.get("https://demo.redevops.io/unsubscribe") == (400, "unsubscribe link is invalid")


def test_the_nightly_probe_reports_a_privately_served_public_journey(monkeypatch):
    mod = _probe_module()
    monkeypatch.setattr(mod, "get", lambda url: (401, ""))          # someone put auth on a demo page
    assert mod.main() == 1


def test_the_nightly_workflow_exists_and_runs_the_probe_and_the_live_checks():
    wf = ROOT / ".github" / "workflows" / "public-demo-nightly.yml"
    assert wf.is_file(), "the contract's live checks have no runner — the script's docstring would be a claim"
    txt = wf.read_text(encoding="utf-8")
    assert "schedule:" in txt and "workflow_dispatch:" in txt
    assert "scripts/probe_public_demo.py" in txt and "DEMO_BASE_URL" in txt
    assert "-k live" in txt, "the live side of the two-sided test must be what nightly runs"
    assert "secrets." not in txt, "a read-only public probe needs no credentials"

