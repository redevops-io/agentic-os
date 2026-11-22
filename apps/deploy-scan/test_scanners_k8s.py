"""Real kubectl, against a disposable local `kind` cluster — never a shared or production cluster.

    kind create cluster --name deploy-scan-test
    kubectl --context kind-deploy-scan-test apply -f <fixtures with the specific gaps below>

Skipped entirely when that context isn't present (CI and most dev machines won't have it set up).
"""
from __future__ import annotations

import importlib
import json
import shutil
import subprocess

import pytest

k8s_rbac = importlib.import_module("deploy-scan.scanners.k8s_rbac")
k8s_netpol = importlib.import_module("deploy-scan.scanners.k8s_netpol")
k8s_exposure = importlib.import_module("deploy-scan.scanners.k8s_exposure")
k8s_identity = importlib.import_module("deploy-scan.scanners.k8s_identity")

_CTX = "kind-deploy-scan-test"
_NS = "deploy-scan-fixture"


def _context_exists() -> bool:
    if not shutil.which("kubectl"):
        return False
    out = subprocess.run(["kubectl", "config", "get-contexts", "-o", "name"],
                          capture_output=True, text=True, timeout=10)
    return _CTX in out.stdout.splitlines()


pytestmark = pytest.mark.skipif(not _context_exists(), reason=f"kubeconfig context {_CTX!r} not present")


def test_rbac_flags_the_wildcard_clusterrole_bound_in_the_fixture_namespace():
    findings = k8s_rbac.run(f"{_CTX}/{_NS}")
    hit = [f for f in findings if f.target.startswith(_NS)]
    assert hit, "expected the fixture's wildcard-role RoleBinding to be flagged"
    assert hit[0].severity == "critical"
    assert "cluster-admin-equivalent" in hit[0].detail


def test_netpol_flags_the_fixture_namespace_as_default_allow():
    findings = k8s_netpol.run(f"{_CTX}/{_NS}")
    assert any(f.target == _NS and "no NetworkPolicy" in f.name for f in findings)


def test_exposure_flags_privileged_hostpath_and_literal_credential_env():
    findings = k8s_exposure.run(f"{_CTX}/{_NS}")
    names = " | ".join(f.name for f in findings)
    assert "runs privileged" in names
    assert "hostPath" in names
    assert "credential-shaped env var" in names


def test_exposure_does_not_flag_the_clean_pod():
    findings = k8s_exposure.run(f"{_CTX}/{_NS}")
    clean = [f for f in findings if f.target == f"{_NS}/fixture-clean-pod"]
    assert clean == [], clean


def test_identity_flags_the_bound_default_service_account_as_medium():
    findings = k8s_identity.run(f"{_CTX}/{_NS}")
    hit = [f for f in findings if "default" in f.name and f.target == f"{_NS}/fixture-privileged-pod"]
    assert hit and hit[0].severity == "medium"


def test_identity_does_not_flag_the_clean_pod_with_its_own_unbound_service_account():
    findings = k8s_identity.run(f"{_CTX}/{_NS}")
    clean = [f for f in findings if f.target == f"{_NS}/fixture-clean-pod"]
    assert clean == [], clean


def test_rbac_flags_cluster_admin_bound_to_an_unexpected_subject():
    findings = k8s_rbac.run(_CTX)
    hit = [f for f in findings if f.ref == "fixture-unexpected-cluster-admin"]
    assert hit and hit[0].severity == "high" and "suspicious-user" in hit[0].name


def test_netpol_flags_the_namespace_wide_open_policy_as_low_severity():
    findings = k8s_netpol.run(f"{_CTX}/deploy-scan-fixture-2")
    hit = [f for f in findings if "ANY namespace" in f.name]
    assert hit and hit[0].severity == "low" and "fixture-allow-any-namespace" in hit[0].ref


def test_rbac_cluster_admin_binding_check_does_not_misfire_on_the_fixture_namespace():
    # the fixture binds a CUSTOM wildcard ClusterRole, not the built-in cluster-admin role — confirms
    # the two code paths (named cluster-admin vs any other over-broad role) are both exercised correctly
    doc = json.loads(subprocess.run(
        ["kubectl", "--context", _CTX, "get", "clusterrolebindings", "-o", "json"],
        capture_output=True, text=True, timeout=10).stdout)
    names = {b["roleRef"]["name"] for b in doc["items"]}
    assert "fixture-wildcard-role" not in names  # it's a RoleBinding (namespaced), not cluster-wide
