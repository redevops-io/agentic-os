"""The standalone deliverables stay what they claim: a reproducible, non-root image recipe, loopback-by-default Compose,
hardened Kubernetes manifests, and nothing private or secret in any of them. File-level checks only (no docker, no cluster);
the live checks are deploy/standalone/edge-sentinel/smoke.sh and docs/standalone/edge-sentinel.md."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
DEPLOY = ROOT / "deploy" / "standalone" / "edge-sentinel"
DOCKERFILE = (HERE / "Dockerfile.standalone").read_text()
TEXT_FILES = [HERE / "Dockerfile.standalone", HERE / "requirements.standalone.txt", HERE / "requirements.build.txt",
              *[p for p in DEPLOY.rglob("*") if p.is_file() and p.suffix in (".yml", ".yaml", ".sh", ".example")],
              DEPLOY / ".env.example", ROOT / "docs" / "standalone" / "edge-sentinel.md",
              ROOT / ".github" / "workflows" / "oci-edge-sentinel.yml"]
PRIVATE = re.compile(r"(\b10\.\d+\.\d+\.\d+\b|\b192\.168\.\d+\.\d+\b|\b172\.(1[6-9]|2\d|3[01])\.\d+\.\d+\b|\.svc\.cluster|\.internal\b|proxmox|"
                     r"hashicorp|vault://|secret/data/|BEGIN [A-Z ]*PRIVATE KEY|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})", re.I)


def _docs(path):
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


# ── image recipe ────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_base_image_is_pinned_by_digest_and_nothing_is_latest():
    assert re.search(r"^ARG PYTHON_IMAGE=python:3\.12-slim@sha256:[0-9a-f]{64}$", DOCKERFILE, re.M)
    assert ":latest" not in DOCKERFILE and not re.search(r"\|\s*(ba)?sh(\s|$)", DOCKERFILE)


def test_the_image_runs_as_a_non_root_user_with_a_healthcheck_and_a_source_date_epoch():
    assert re.search(r"^USER 10001:10001$", DOCKERFILE, re.M)
    assert re.search(r"^HEALTHCHECK ", DOCKERFILE, re.M) and "/readyz" in DOCKERFILE
    assert "SOURCE_DATE_EPOCH" in DOCKERFILE
    assert re.search(r"^VOLUME \[\"/data\"\]", DOCKERFILE, re.M)


def test_the_kernel_is_built_from_the_checkout_not_from_a_tarball_of_another_ref():
    assert "COPY agentic_os ./agentic_os" in DOCKERFILE and "archive/" not in DOCKERFILE and "KERNEL_REF" not in DOCKERFILE


def test_the_standalone_profile_defaults_are_the_secure_ones():
    for want in ("SENTINEL_REQUIRE_AUTH=1", "AGENTIC_OS_PROFILE=production", "AGENTIC_OS_AUTHORITY_DB=/data/authority.db",
                 "SENTINEL_STATE_DIR=/data/state"):
        assert want in DOCKERFILE, want
    assert "SENTINEL_BLOCK_ENABLED=" not in DOCKERFILE.replace("SENTINEL_BLOCK_ENABLED unset", "")      # real execution is never on by default
    assert "SENTINEL_ALLOW_UNAUTHENTICATED_EXPOSURE" not in DOCKERFILE.replace("#", "")


def test_the_scanners_are_optional_pinned_and_verified_for_both_architectures():
    assert "ARG WITH_SCANNERS=0" in DOCKERFILE
    for name in ("TRIVY_SHA256_AMD64", "TRIVY_SHA256_ARM64", "SYFT_SHA256_AMD64", "SYFT_SHA256_ARM64"):
        assert re.search(rf"^ARG {name}=[0-9a-f]{{64}}$", DOCKERFILE, re.M), name
    assert "sha256sum -c" in DOCKERFILE and "docker.io/library/docker" not in DOCKERFILE


@pytest.mark.parametrize("name", ["requirements.standalone.txt", "requirements.build.txt"])
def test_every_requirement_is_pinned_exactly(name):
    lines = [l.strip() for l in (HERE / name).read_text().splitlines() if l.strip() and not l.startswith("#")]
    assert lines
    for l in lines:
        assert re.fullmatch(r"[A-Za-z0-9_.\-]+==[0-9][A-Za-z0-9.]*", l) or re.fullmatch(r"[A-Za-z0-9_.\-]+ @ git\+https://github\.com/redevops-io/[a-z\-]+\.git@[0-9a-f]{40}", l) \
            or re.fullmatch(r"[A-Za-z0-9_.\-]+ @ https://github\.com/redevops-io/[a-z\-]+/archive/[0-9a-f]{40}\.tar\.gz", l), l


def test_the_lock_covers_what_the_app_imports():
    pinned = {re.split(r"[ =@]", l.strip())[0].lower().replace("_", "-") for l in (HERE / "requirements.standalone.txt").read_text().splitlines()
              if l.strip() and not l.startswith("#")}
    for needed in ("fastapi", "uvicorn", "httpx", "pymysql", "pyyaml", "pydantic", "cryptography", "runtime-contracts", "discovery-runtime",
                   "knowledge-frontier", "context-runtime"):
        assert needed in pinned, needed


# ── Compose ─────────────────────────────────────────────────────────────────────────────────────────────────────

def test_compose_publishes_on_loopback_by_default_and_hardens_the_app():
    c = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text())
    app = c["services"]["edge-sentinel"]
    assert any(str(p).startswith("${EDGE_SENTINEL_BIND:-127.0.0.1}:") for p in app["ports"])
    assert app["read_only"] is True and app["cap_drop"] == ["ALL"] and "no-new-privileges:true" in app["security_opt"]
    assert "ports" not in c["services"]["crowdsec"], "the CrowdSec API is not published"
    env = app["environment"]
    assert env["SENTINEL_BASIC_AUTH_PASS_FILE"].startswith("/run/secrets/") and "SENTINEL_BASIC_AUTH_PASS" not in env
    assert set(c["secrets"]) == {"operator_password", "crowdsec_bouncer_key", "crowdsec_machine_password"}
    assert re.search(r"@sha256:[0-9a-f]{64}$", c["services"]["crowdsec"]["image"])


def test_env_example_lists_names_only():
    for line in (DEPLOY / ".env.example").read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            name, _, value = line.partition("=")
            assert re.fullmatch(r"[A-Z][A-Z0-9_]*", name), line
            assert not re.search(r"(PASS|KEY|SECRET|TOKEN)", name), f"a secret-looking name in .env.example: {name}"
            assert len(value) < 60


def test_the_secrets_directory_is_never_committed():
    assert "secrets/" in (DEPLOY / ".gitignore").read_text() and ".env" in (DEPLOY / ".gitignore").read_text()
    assert not (DEPLOY / "secrets").exists() and not (DEPLOY / ".env").exists()


# ── Kubernetes ──────────────────────────────────────────────────────────────────────────────────────────────────

def _workloads():
    for f in ("edge-sentinel.yaml", "crowdsec.yaml"):
        for d in _docs(DEPLOY / "k8s" / f):
            yield f, d


def test_every_manifest_parses_and_stays_in_the_namespace():
    for f in sorted((DEPLOY / "k8s").glob("*.yaml")):
        for d in _docs(f):
            assert d["apiVersion"] and d["kind"]
            if d["kind"] not in ("Namespace", "Kustomization"):
                assert d["metadata"]["namespace"] == "edge-sentinel", (f.name, d["metadata"]["name"])


def test_the_sentinel_pod_meets_the_restricted_profile():
    dep = next(d for f, d in _workloads() if d["kind"] == "Deployment" and d["metadata"]["name"] == "edge-sentinel")
    spec = dep["spec"]["template"]["spec"]
    sc, csc = spec["securityContext"], spec["containers"][0]["securityContext"]
    assert sc["runAsNonRoot"] is True and sc["runAsUser"] == 10001 and sc["seccompProfile"]["type"] == "RuntimeDefault"
    assert csc["readOnlyRootFilesystem"] is True and csc["allowPrivilegeEscalation"] is False and csc["capabilities"]["drop"] == ["ALL"]
    assert spec["automountServiceAccountToken"] is False and dep["spec"]["replicas"] == 1
    env = {e["name"]: e for e in spec["containers"][0]["env"]}
    assert "SENTINEL_BASIC_AUTH_PASS" not in env and env["SENTINEL_BASIC_AUTH_PASS_FILE"]["value"].startswith("/run/secrets/")
    assert spec["containers"][0]["readinessProbe"]["httpGet"]["path"] == "/readyz"


def test_images_are_pinned_never_latest():
    for _f, d in _workloads():
        if d["kind"] == "Deployment":
            img = d["spec"]["template"]["spec"]["containers"][0]["image"]
            assert not img.endswith(":latest") and (":" in img.split("/")[-1] or "@sha256:" in img), img
    k = yaml.safe_load((DEPLOY / "k8s" / "kustomization.yaml").read_text())
    assert re.fullmatch(r"\d+\.\d+\.\d+", k["images"][0]["newTag"])


def test_the_secret_example_carries_keys_and_no_values_and_is_not_applied_by_the_kustomization():
    s = _docs(DEPLOY / "k8s" / "secret.example.yaml")[0]
    assert set(s["stringData"]) == {"operator-password", "crowdsec-bouncer-key", "crowdsec-machine-password"}
    assert all(v == "" for v in s["stringData"].values())
    k = yaml.safe_load((DEPLOY / "k8s" / "kustomization.yaml").read_text())
    assert "secret.example.yaml" not in k["resources"] and "ingress.example.yaml" not in k["resources"]


def test_the_network_policy_denies_by_default_and_the_ingress_example_uses_a_placeholder_host():
    pols = _docs(DEPLOY / "k8s" / "networkpolicy.yaml")
    assert any(p["metadata"]["name"] == "default-deny" and p["spec"]["podSelector"] == {} for p in pols)
    ing = _docs(DEPLOY / "k8s" / "ingress.example.yaml")[0]
    assert ing["spec"]["rules"][0]["host"] == "sentinel.example.com"


# ── nothing private anywhere ────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", [p for p in TEXT_FILES if p.exists()], ids=lambda p: p.name)
def test_no_internal_topology_or_secret_material(path):
    hit = PRIVATE.search(path.read_text())
    assert hit is None, f"{path.name}: {hit.group(0)}"


def test_the_bootstrap_scripts_never_let_cscli_print_the_credentials_it_registers():
    """`cscli machines add -f -` writes the machine credentials to stderr (and `kubectl exec` forwards both streams)."""
    for path in (DEPLOY / "bootstrap.sh", DEPLOY / "k8s" / "bootstrap.sh"):
        text = path.read_text()
        line = next(l for l in text.splitlines() if "machines add" in l and not l.lstrip().startswith("#"))
        assert ">/dev/null 2>&1" in line, f"{path.name}: {line}"
