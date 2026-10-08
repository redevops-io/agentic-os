"""Suite server: serves a suite as one surface composing the apps' operators."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agentic_os.app_kit import (
    AppManifest,
    CapabilityRef,
    Suite,
    VerifierRef,
    build_suite_app,
)
from agentic_os.mission.operator_sdk import Operator, capability


def _op(name, caps):
    return Operator(name, [capability(c, lambda i: {"did": c}, provides=[c.split(".")[-1]]) for c in caps])


def _manifest(name, caps):
    return AppManifest(name=name, capabilities=tuple(CapabilityRef(c) for c in caps))


def _setup():
    ops = {"app-a": _op("app-a", ["a.read", "a.act"]), "app-b": _op("app-b", ["b.do"])}
    mans = {"app-a": _manifest("app-a", ["a.read", "a.act"]), "app-b": _manifest("app-b", ["b.do"])}
    suite = Suite(name="demo-suite", apps=("app-a", "app-b"), mission_templates=("t1",),
                  description="demo composition")
    return suite, ops, mans


def test_suite_manifest_lists_composed_capabilities():
    suite, ops, mans = _setup()
    client = TestClient(build_suite_app(suite, ops, manifests=mans))
    m = client.get("/suite").json()
    assert m["suite"] == "demo-suite"
    assert m["apps"] == ["app-a", "app-b"]
    assert set(m["capabilities"]) == {"a.read", "a.act", "b.do"}      # union of the apps' caps
    assert m["surfaces"] == ["/m/app-a", "/m/app-b"]
    assert m["mission_templates"] == ["t1"]


def test_each_apps_operator_is_mounted_and_invokable():
    suite, ops, mans = _setup()
    client = TestClient(build_suite_app(suite, ops, manifests=mans))
    caps_a = client.get("/m/app-a/capabilities").json()
    assert any(c["name"] == "a.act" for c in caps_a["capabilities"])
    r = client.post("/m/app-b/invoke", json={"capability": "b.do", "inputs": {}})
    assert r.json()["result"]["did"] == "b.do"
    assert client.get("/health").json()["ok"] is True


def test_missing_operator_is_rejected():
    suite, ops, mans = _setup()
    with pytest.raises(ValueError, match="missing operators"):
        build_suite_app(Suite(name="x", apps=("app-a", "app-z")), ops, manifests=mans)


def test_conformance_gated_fails_closed_on_nonconformant_app():
    # app-a declares a side-effecting cap with NO verifier -> N4 fail at suite registration
    ops = {"app-a": Operator("app-a", [capability("a.write", lambda i: {}, side_effecting=True)])}
    mans = {"app-a": AppManifest(name="app-a", capabilities=(CapabilityRef("a.write"),))}  # no verifier
    with pytest.raises(Exception):
        build_suite_app(Suite(name="s", apps=("app-a",)), ops, manifests=mans)


def test_without_manifests_still_serves_capability_union():
    suite, ops, _ = _setup()
    client = TestClient(build_suite_app(suite, ops))      # no conformance gating, still composes
    assert set(client.get("/suite").json()["capabilities"]) == {"a.read", "a.act", "b.do"}
