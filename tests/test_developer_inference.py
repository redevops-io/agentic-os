"""Local model runs in an isolated developer environment: a person may preauthorize them for one agent, for a time, a
number of times and a budget, only for a model that is on the deployment's own infrastructure and costs nothing per use.
Anything paid or hosted, and any condition that is missing, waits for a person."""
from __future__ import annotations

import json
import threading

import pytest

pytest.importorskip("httpx")
import fastapi  # noqa: F401,PLC0415 - required on this line: its absence is a failure, not a skip

from agentic_os.governed_writes import preauthorization as P  # noqa: E402
from tests.test_agent_gateway_media import APPROVER, LIMITS, world  # noqa: E402,F401

URL = "/api/writes/preauthorizations"
LOCAL = {"local": True, "costly": False, "cost_usd": 0.0}
DEV_LIMITS = {**LIMITS, "acme": {"policy_refs": [*LIMITS["acme"]["policy_refs"], "writes:preauthorize", "inference:local"]}}
GRANT = {"agent": "agent:m0", "actions": ["media.request_generate"], "max_uses": 10, "minutes": 30,
         "reason": "trying the local video model", "budget": {"video_seconds": 10}}


@pytest.fixture
def developer(world, monkeypatch):  # noqa: F811
    for k, v in {P.ENV_ENABLED: "1", P.ENV_ENVIRONMENT: "isolated", P.ENV_LOCAL_BACKENDS: "open-sora-v2",
                 "AGENTIC_OS_TENANT_LIMITS": json.dumps(DEV_LIMITS)}.items():
        monkeypatch.setenv(k, v)
    world.studio.backend, world.studio.inference = "open-sora-v2", dict(LOCAL)
    return world


def grant(w, **change):
    return w.client.post(URL, headers=APPROVER, json={**GRANT, **change})


def ask(w, **more):
    return w.ok("media.request_generate", kind="video", prompt="a lighthouse at dusk", **more)


def generated(w):
    return [c for c in w.studio.calls if c[0] == "visual.generate"]


def state_of(w, ident):
    return next(p for p in w.client.get(URL, headers=APPROVER).json()["preauthorizations"] if p["id"] == ident)


def test_a_local_run_inside_the_budget_needs_no_second_approval_and_the_budget_stops_it(developer):
    w = developer
    made = grant(w)
    assert made.status_code == 200, made.text
    ident = made.json()["id"]
    assert made.json()["budget"] == {"video_seconds": 10.0} and made.json()["spent"] == {}
    first = ask(w, duration=4)
    assert first["state"] == "executed_verified" and first["decided_by"].startswith(f"preauthorization:{ident}"), first
    assert w.studio.approved_by.startswith(f"preauthorization:{ident}") and generated(w)[-1][1]["_tenant"] == "acme"
    assert state_of(w, ident)["spent"] == {"video_seconds": 4.0}
    second = ask(w)                                                      # no length asked: the service's 5 s are charged and sent
    assert second["state"] == "executed_verified" and generated(w)[-1][1]["duration"] == 5.0
    assert state_of(w, ident)["spent"] == {"video_seconds": 9.0}
    third = ask(w, duration=1.5)                                         # 1 s is left
    assert third["state"] == "pending" and len(generated(w)) == 2
    assert w.decide(third)["state"] == "executed_verified"               # a person may still approve it
    assert state_of(w, ident)["spent"] == {"video_seconds": 9.0}


def test_a_video_budget_does_not_cover_an_image(developer):
    w = developer
    assert grant(w).status_code == 200
    out = w.ok("media.request_generate", kind="image", prompt="a lighthouse")
    assert out["state"] == "pending" and not generated(w)


@pytest.mark.parametrize("why", ["hosted", "billed", "costly", "says nothing", "not declared by the deployment", "nothing declared",
                                 "environment not isolated", "developer mode off", "tenant lacks inference:local", "cost of the wrong type"])
def test_any_missing_condition_leaves_the_run_for_a_person(developer, monkeypatch, why):
    w = developer
    ident = grant(w).json()["id"]
    if why == "hosted":
        w.studio.inference = {**LOCAL, "local": False}
    elif why == "billed":
        w.studio.inference = {**LOCAL, "cost_usd": 0.04}
    elif why == "costly":
        w.studio.inference = {**LOCAL, "costly": True}
    elif why == "says nothing":
        w.studio.inference = None
    elif why == "cost of the wrong type":
        w.studio.inference = {**LOCAL, "cost_usd": False}
    elif why == "not declared by the deployment":
        monkeypatch.setenv(P.ENV_LOCAL_BACKENDS, "some-other-model")
    elif why == "nothing declared":
        monkeypatch.delenv(P.ENV_LOCAL_BACKENDS)
    elif why == "environment not isolated":
        monkeypatch.setenv(P.ENV_ENVIRONMENT, "production")
    elif why == "developer mode off":
        monkeypatch.delenv(P.ENV_ENABLED)
    else:
        refs = [r for r in DEV_LIMITS["acme"]["policy_refs"] if r != "inference:local"]
        monkeypatch.setenv("AGENTIC_OS_TENANT_LIMITS", json.dumps({**DEV_LIMITS, "acme": {"policy_refs": refs}}))
    out = ask(w, duration=2)
    assert out["state"] == "pending" and not generated(w), (why, out)
    monkeypatch.setenv(P.ENV_ENABLED, "1")
    assert state_of(w, ident)["uses"] == 0 and state_of(w, ident)["spent"] == {}, why


def test_a_person_can_still_approve_a_paid_run_and_the_card_says_it_is_paid(developer):
    w = developer
    w.studio.inference = {"local": False, "costly": True, "cost_usd": 0.04}
    grant(w)
    asked = ask(w, duration=2)
    assert asked["state"] == "pending"
    card = w.client.get(f"/api/writes/{asked['request_id']}", headers=APPROVER).json()
    assert "outside provider" in json.dumps(card["evidence"]) and "billed (about $0.04)" in json.dumps(card["evidence"]), card
    assert w.decide(asked)["state"] == "executed_verified" and w.studio.approved_by == "approver:acme"


@pytest.mark.parametrize("who", ["preauthorization", "person"])
def test_a_model_that_changes_between_the_request_and_the_run_is_not_run(developer, who):
    w = developer
    if who == "preauthorization":
        ident = grant(w).json()["id"]
    seen = []

    def shift():
        seen.append(1)
        if len(seen) > 1:                                                # free when asked about, billed when about to run
            w.studio.inference = {**LOCAL, "cost_usd": 0.5, "costly": True}

    w.studio.on_health = shift
    asked = ask(w, duration=2)
    if who == "person":
        asked = w.decide(asked)
    final = w.ok("media.request_status", request_id=asked["request_id"])
    assert not generated(w) and final["state"] != "executed_verified" and len(seen) >= 2, final


@pytest.mark.parametrize("change,why", [
    ({"budget": None}, "a budget goes with"),
    ({"actions": ["erp.request_comment"]}, "a budget goes with"),
    ({"budget": {}}, "video_seconds and/or images"),
    ({"budget": {"tokens": 5}}, "video_seconds and/or images"),
    ({"budget": {"video_seconds": 0}}, "whole number"),
    ({"budget": {"video_seconds": P.BUDGET_MAX["video_seconds"] + 1}}, "whole number"),
    ({"budget": {"images": True}}, "whole number"),
    ({"budget": {"images": 2.5}}, "whole number"),
])
def test_what_a_model_budget_may_not_be(developer, change, why):
    out = grant(developer, **change)
    assert out.status_code == 422 and why in out.text, out.text


@pytest.mark.parametrize("missing", [P.ENV_ENVIRONMENT, "inference:local"])
def test_a_model_run_cannot_be_preauthorized_outside_an_isolated_developer_environment(developer, monkeypatch, missing):
    if missing == P.ENV_ENVIRONMENT:
        monkeypatch.delenv(missing)
    else:
        refs = [r for r in DEV_LIMITS["acme"]["policy_refs"] if r != missing]
        monkeypatch.setenv("AGENTIC_OS_TENANT_LIMITS", json.dumps({**DEV_LIMITS, "acme": {"policy_refs": refs}}))
    out = grant(developer)
    assert out.status_code == 422 and "runs a model" in out.text, out.text


def test_publishing_stays_with_a_person_even_here(developer):
    w = developer
    assert grant(w, actions=["media.request_generate", "social.request_post"]).status_code == 200
    from tests.test_agent_gateway_media import later
    final = w.reel()
    out = w.ok("social.request_post", channel_id="ig1", caption="hello", when=later(), asset=final["asset"])
    assert out["state"] == "pending" and not w.social.posts and not w.social.uploads


def test_many_runs_at_once_cannot_spend_more_than_the_budget(developer):
    w = developer
    made = grant(w, budget={"video_seconds": 12}).json()
    results = []

    def one(n):
        out = w.call("media.request_generate", kind="video", prompt=f"clip {n}", duration=5, idempotency_key=f"race-{n:06d}")
        results.append(out["output"]["state"])

    threads = [threading.Thread(target=one, args=(n,)) for n in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results).count("executed_verified") == 2 and results.count("pending") == 4, results
    assert state_of(w, made["id"])["spent"] == {"video_seconds": 10.0} and len(generated(w)) == 2


def test_a_refused_decision_returns_the_use_and_the_budget(developer, monkeypatch):
    w = developer
    ident = grant(w).json()["id"]
    from agentic_os import app_gate
    monkeypatch.setattr(app_gate, "operator_denial", lambda *a, **k: "the deciding identity may not execute this")
    out = ask(w, duration=3)
    assert out["state"] == "pending" and not generated(w)
    after = state_of(w, ident)
    assert after["uses"] == 0 and after["spent"] == {"video_seconds": 0.0}, after
