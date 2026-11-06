"""Container acceptance criteria for the reference Helm chart (docs/release/CONTAINER_ACCEPTANCE_CRITERIA.md).

Two layers:

* STATIC tests read ``values.yaml``, the templates and the docs as text/YAML. They need no ``helm`` binary and always run.
* RENDERED tests run ``helm template`` and assert every criterion on the manifests that would be applied. They are skipped
  (with the reason) only when no helm binary is found: set ``HELM_BIN`` or put ``helm`` (v3 or v4) on PATH.

Nothing here contacts a cluster or a registry.

Criteria enforced (the owner's list, 2026-11-06): non-root containers, dropped capabilities, read-only root filesystem
(with explicit writable mounts) where feasible, explicit health/readiness/startup checks, immutable image references,
secure network defaults (ClusterIP, no Ingress/LoadBalancer/NodePort, default-deny NetworkPolicy, no host mounts).
The chart has NO default image: rendering must fail without ``runtime.image.digest`` or ``runtime.image.tag``.
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess

import pytest

yaml = pytest.importorskip("yaml")

ROOT = pathlib.Path(__file__).resolve().parents[1]
CHART = ROOT / "deploy" / "reference-system" / "redevops-runtime"
TEMPLATES = CHART / "templates"
VALUES = CHART / "values.yaml"
HELM = os.environ.get("HELM_BIN") or shutil.which("helm")
needs_helm = pytest.mark.skipif(HELM is None, reason="no helm binary: set HELM_BIN or put helm (v3/v4) on PATH; the static tests still run")

DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64
# Public digests of the deprecated 0.3.0 / 0.3.1 / v6 images (index and amd64 manifest), see docs/release/OCI_RELEASE_RUNBOOK.md.
VULN_DIGESTS = [
    "sha256:9d0315d8f49711ee2f41e027252fb5d0b3cdaeea91b9bed57bd6d10cffef6cf6",
    "sha256:86f91365bab7ee8e604ed5854aa0d56881ba984ec834deab3b4d6d5ec22d7e56",
    "sha256:a134be71553621861aee2b56dd434aa5d19432c6df7dcdeb8360b23146a22fe1",
    "sha256:cabbdd8d1c5acdc9ba7c2e371ea2dfdc49241d36a7a7ada1af9cfe31974fd7ab",
]
# Containers that may deviate from the strict defaults, with the justification kept in the criteria doc (section "Exceptions").
# The ROCm vLLM server: GPU image, user and cache paths not verified for non-root/read-only; opt-in, off by default.
EXCEPTIONS = {"vllm": {"readOnlyRootFilesystem", "runAsNonRoot"}}


def _values() -> dict:
    return yaml.safe_load(VALUES.read_text())


def _chart_text(include_helpers: bool = False) -> dict[str, str]:
    out = {"values.yaml": VALUES.read_text()}
    for f in sorted(TEMPLATES.glob("*")):
        if f.name == "_helpers.tpl" and not include_helpers:
            continue
        out[f"templates/{f.name}"] = f.read_text()
    return out


# ────────────────────────────── static layer (always runs) ──────────────────────────────


class TestStaticDefaults:
    def test_there_is_no_default_image_reference(self):
        v = _values()
        assert v["runtime"]["image"]["tag"] == "" and v["runtime"]["image"]["digest"] == ""
        assert v["security"]["image"]["tag"] == "" and v["security"]["image"]["digest"] == ""
        assert v["cockpit"]["image"]["tag"] == "" and v["cockpit"]["image"]["digest"] == ""

    def test_values_and_templates_name_no_deprecated_or_floating_image_tag(self):
        bad = []
        for name, text in _chart_text().items():          # _helpers.tpl holds the deny-list itself and is excluded
            for ln, line in enumerate(text.splitlines(), 1):
                code = line.split("#", 1)[0]
                if re.search(r"""(tag|image)\s*:\s*["']?[^\s"']*(:latest|:v6|:0\.3\.[01]|\blatest\b)["']?\s*$""", code) \
                        or re.search(r"""\btag\s*:\s*["']?(v6|0\.3\.0|0\.3\.1|latest|stable)["']?\s*$""", code):
                    bad.append((name, ln, line.strip()))
        assert bad == []

    def test_chart_metadata_does_not_advertise_a_deprecated_app_version(self):
        meta = yaml.safe_load((CHART / "Chart.yaml").read_text())
        assert str(meta.get("appVersion", "")) not in {"0.3.0", "0.3.1", "v6"}

    def test_helpers_reject_missing_mutable_and_deprecated_references(self):
        h = (TEMPLATES / "_helpers.tpl").read_text()
        assert 'define "rr.imageRef"' in h and "fail" in h
        for needle in ("latest", "v6", "0.3.0", "0.3.1", *VULN_DIGESTS):
            assert needle in h, needle
        assert "sha256:[a-f0-9]{64}" in h

    def test_every_workload_uses_the_image_helper(self):
        for f in TEMPLATES.glob("*.yaml"):
            for m in re.finditer(r"^\s*image:\s*(.+)$", f.read_text(), re.M):
                assert any(h in m.group(1) for h in ("rr.imageRef", "rr.runtimeImage", "rr.pinnedImage")), (f.name, m.group(0))

    def test_hardening_defaults_in_values(self):
        r = _values()["runtime"]
        sc, psc = r["securityContext"], r["podSecurityContext"]
        assert sc["runAsNonRoot"] is True and sc["runAsUser"] == 10001 and sc["runAsGroup"] == 10001
        assert sc["allowPrivilegeEscalation"] is False and sc["privileged"] is False
        assert sc["capabilities"]["drop"] == ["ALL"] and "add" not in sc["capabilities"]
        assert sc["readOnlyRootFilesystem"] is True
        assert sc["seccompProfile"]["type"] == "RuntimeDefault" and psc["seccompProfile"]["type"] == "RuntimeDefault"
        assert psc["runAsNonRoot"] is True and psc["runAsUser"] == 10001
        assert r["image"]["pullPolicy"] == "IfNotPresent"
        assert r["service"]["type"] == "ClusterIP"
        assert r["resources"]["requests"] and r["resources"]["limits"]
        assert set(r["probes"]) >= {"startup", "liveness", "readiness"}
        assert _values()["serviceAccount"]["automountServiceAccountToken"] is False
        assert _values()["networkPolicy"]["enabled"] is True

    def test_runtime_template_declares_the_three_probes_on_health_and_writable_mounts(self):
        t = (TEMPLATES / "runtime.yaml").read_text()
        for p in ("startupProbe", "readinessProbe", "livenessProbe"):
            assert p in t
        assert t.count("path: /health") >= 3
        assert "emptyDir" in t and "automountServiceAccountToken" in t

    def test_control_token_comes_from_a_secret_never_from_values(self):
        t = (TEMPLATES / "runtime.yaml").read_text()
        assert "AGENTIC_OS_CONTROL_TOKEN" in t and "secretKeyRef" in t
        assert not re.search(r"\.Values\.runtime\.controlToken\b(?!Secret)", t)
        assert _values()["runtime"]["controlTokenSecret"] == ""

    def test_no_host_access_ingress_or_exposed_service_in_templates_or_values(self):
        for name, text in _chart_text().items():
            body = "\n".join(l.split("#", 1)[0] for l in text.splitlines())
            assert not re.search(r"\bhost(Network|PID|IPC)\s*:\s*true", body), name
            assert not re.search(r"\bhostPath\b|docker\.sock|\bhostPort\b", body), name
            assert not re.search(r"\bprivileged\s*:\s*true", body), name
            assert not re.search(r"type\s*:\s*(LoadBalancer|NodePort)\b", body), name
            assert "kind: Ingress" not in body and "kind: Gateway" not in body, name
            assert "add:" not in body or "capabilities" not in body, name

    def test_a_networkpolicy_template_exists(self):
        t = (TEMPLATES / "networkpolicy.yaml").read_text()
        assert "kind: NetworkPolicy" in t and "policyTypes" in t and "Ingress" in t and "Egress" in t

    def test_docs_and_ci_examples_do_not_install_a_deprecated_or_floating_image(self):
        offenders = []
        scan = [CHART / "README.md", CHART / "templates" / "NOTES.txt", ROOT / "docker-compose.yml"]
        scan += [p for p in (ROOT / ".github").rglob("*.y*ml")] if (ROOT / ".github").exists() else []
        for f in scan:
            if not f.exists():
                continue
            for ln, line in enumerate(f.read_text().splitlines(), 1):
                if re.search(r"redevops-runtime(:|@sha256:)(0\.3\.[01]|v6|latest)\b|cockpit:v6|--set[^\n]*image\.tag=(0\.3\.[01]|v6|latest)", line):
                    offenders.append((str(f.relative_to(ROOT)), ln))
        assert offenders == []

    def test_root_compose_publishes_on_loopback_only_and_mounts_no_docker_socket(self):
        text = (ROOT / "docker-compose.yml").read_text()
        for m in re.finditer(r"^\s*ports:\s*\[(.*)\]", text, re.M):
            for p in re.findall(r"\"([^\"]+)\"", m.group(1)):
                assert re.match(r"^(\$\{AOS_BIND_ADDR:-127\.0\.0\.1\}|127\.0\.0\.1):", p), p
        body = "\n".join(l.split("#", 1)[0] for l in text.splitlines())
        assert "docker.sock" not in body and "privileged: true" not in body

    @pytest.mark.xfail(
        condition=not (ROOT / "scripts" / "release" / "oci_prepare.sh").exists(), strict=True,
        reason="this line's Dockerfile is not yet hardened (floating base, root, no HEALTHCHECK): converge the OCI hardening "
               "from post-rc/oci-release before the release (docs/release/CONTAINER_ACCEPTANCE_CRITERIA.md, section 7)")
    def test_dockerfile_meets_the_image_criteria(self):
        for name in ("Dockerfile", "Dockerfile.projects"):
            f = ROOT / name
            if not f.exists():
                continue
            text = f.read_text()
            froms = re.findall(r"^FROM\s+(\S+)", text, re.M)
            assert froms and all("@sha256:" in x for x in froms), (name, froms)
            users = re.findall(r"^USER\s+(\S+)", text, re.M)
            assert users and not users[-1].startswith(("root", "0")), (name, users)
            assert re.search(r"^HEALTHCHECK\b", text, re.M), name
            assert not re.search(r"\bcurl\b[^\n]*\|\s*(ba)?sh", text), name

    def test_criteria_doc_exists_and_names_the_six_criteria(self):
        d = (ROOT / "docs" / "release" / "CONTAINER_ACCEPTANCE_CRITERIA.md").read_text().lower()
        for k in ("non-root", "capabilit", "read-only root", "health", "digest", "networkpolicy", "loopback"):
            assert k in d, k


# ────────────────────────────── rendered layer (needs helm) ──────────────────────────────


def _base_sets(extra_values: dict | None = None) -> list[str]:
    sets = ["runtime.image.digest=" + DIGEST]
    if "permissions" in _values():                       # the main line still requires the grant-store key Secret
        sets.append("permissions.keySecret=perm-key")
    return sets


def _run(sets=(), *, base=True, release="rel", ns="apps") -> subprocess.CompletedProcess:
    cmd = [HELM, "template", release, str(CHART), "--namespace", ns]
    for s in (_base_sets() if base else []) + list(sets):
        cmd += ["--set", s]
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("TMPDIR", "/tmp")}
    return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120)


def render(sets=(), **kw) -> list[dict]:
    p = _run(sets, **kw)
    assert p.returncode == 0, p.stderr
    return [d for d in yaml.safe_load_all(p.stdout) if d]


def render_fails(sets=(), **kw) -> str:
    p = _run(sets, **kw)
    assert p.returncode != 0, "render unexpectedly succeeded"
    return p.stderr


def kinds(docs, kind):
    return [d for d in docs if d["kind"] == kind]


def pod_specs(docs):
    for d in docs:
        if d["kind"] in ("Deployment", "StatefulSet", "DaemonSet"):
            yield d["metadata"]["name"], d["spec"]["template"]["spec"]
        elif d["kind"] == "Job":
            yield d["metadata"]["name"], d["spec"]["template"]["spec"]


def containers(docs):
    for name, spec in pod_specs(docs):
        for c in spec.get("containers", []) + spec.get("initContainers", []):
            yield name, spec, c


def _runtime(docs):
    dep = [d for d in kinds(docs, "Deployment") if d["metadata"]["labels"].get("app.kubernetes.io/component") == "runtime"]
    assert len(dep) == 1
    return dep[0], dep[0]["spec"]["template"]["spec"], dep[0]["spec"]["template"]["spec"]["containers"][0]


def _effective(spec, c, key):
    """Container value wins, else pod value (securityContext fields that exist at both levels)."""
    return c.get("securityContext", {}).get(key, spec.get("securityContext", {}).get(key))


@needs_helm
class TestNoDefaultImage:
    def test_render_fails_without_any_image_reference_with_a_clear_message(self):
        err = render_fails(base=False, sets=["permissions.keySecret=perm-key"] if "permissions" in _values() else [])
        assert "runtime.image.digest" in err and "required" in err and "no default" in err.lower()

    def test_the_failure_is_the_image_and_not_something_else(self):
        err = render_fails(base=False, sets=["permissions.keySecret=perm-key"] if "permissions" in _values() else [])
        assert "runtime.image.tag" in err

    @pytest.mark.parametrize("tag", ["latest", "v6", "0.3.1", "0.3.0", "stable", "main", "1", "v1.2.3", ""])
    def test_floating_or_deprecated_tags_are_refused(self, tag):
        base = ["permissions.keySecret=perm-key"] if "permissions" in _values() else []
        err = render_fails(base=False, sets=base + [f"runtime.image.tag={tag}"])
        assert "runtime.image" in err

    @pytest.mark.parametrize("digest", VULN_DIGESTS)
    def test_deprecated_digests_are_refused(self, digest):
        assert "deprecated" in render_fails([f"runtime.image.digest={digest}"]).lower()

    @pytest.mark.parametrize("digest", ["sha256:abc", "md5:" + "a" * 32, "sha256:" + "A" * 64, "a" * 64])
    def test_malformed_digests_are_refused(self, digest):
        assert "sha256" in render_fails([f"runtime.image.digest={digest}"])

    def test_a_digest_renders_as_repo_at_digest(self):
        _, spec, c = _runtime(render())
        assert c["image"] == "ghcr.io/redevops-io/redevops-runtime@" + DIGEST
        assert c["imagePullPolicy"] == "IfNotPresent"

    def test_a_version_tag_is_accepted_and_the_digest_wins_when_both_are_set(self):
        _, _, c = _runtime(render(base=False, sets=["runtime.image.tag=0.5.0"] + (["permissions.keySecret=k"] if "permissions" in _values() else [])))
        assert c["image"] == "ghcr.io/redevops-io/redevops-runtime:0.5.0"
        _, _, c = _runtime(render(["runtime.image.tag=0.5.0"]))
        assert c["image"].endswith("@" + DIGEST)

    def test_no_rendered_image_is_floating_or_untagged(self):
        docs = render(["retriever.pgvector.bundled.enabled=true", "retriever.pgvector.bundled.passwordSecret=pg",
                       "cockpit.enabled=true", "cockpit.image.digest=" + OTHER_DIGEST,
                       "model.server.enabled=true", "model.server.image=rocm/vllm@" + OTHER_DIGEST])
        seen = [c["image"] for _, _, c in containers(docs)]
        assert len(seen) >= 4
        for img in seen:
            assert "@sha256:" in img or re.search(r":[0-9]+\.[0-9]+\.[0-9]+", img), img
            assert not re.search(r":(latest|v6|0\.3\.[01])$", img), img


@pytest.fixture(scope="module")
def full():
    """Every optional workload on, to prove each one is hardened."""
    if HELM is None:
        pytest.skip("no helm binary")
    return render(["retriever.pgvector.bundled.enabled=true", "retriever.pgvector.bundled.passwordSecret=pg",
                   "cockpit.enabled=true", "cockpit.image.digest=" + OTHER_DIGEST,
                   "model.server.enabled=true", "model.server.image=rocm/vllm@" + OTHER_DIGEST,
                   "runtime.controlTokenSecret=ctl"])


@needs_helm
class TestRenderedHardening:
    def test_every_pod_is_non_root_with_seccomp_and_no_host_access(self, full):
        names = []
        for name, spec in pod_specs(full):
            names.append(name)
            for k in ("hostNetwork", "hostPID", "hostIPC"):
                assert not spec.get(k), (name, k)
            assert spec.get("automountServiceAccountToken") is False, name
            assert spec["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault", name
            for v in spec.get("volumes", []):
                assert "hostPath" not in v, (name, v)
        assert len(names) >= 5

    def test_every_container_drops_all_capabilities_and_blocks_escalation(self, full):
        seen = 0
        for name, spec, c in containers(full):
            sc = c["securityContext"]
            assert sc["capabilities"]["drop"] == ["ALL"] and not sc["capabilities"].get("add"), (name, c["name"])
            assert sc["allowPrivilegeEscalation"] is False and not sc.get("privileged"), (name, c["name"])
            assert sc.get("seccompProfile", spec["securityContext"].get("seccompProfile"))["type"] == "RuntimeDefault"
            seen += 1
        assert seen >= 5

    def test_every_container_is_non_root_with_a_read_only_root_filesystem_except_the_documented_exceptions(self, full):
        for name, spec, c in containers(full):
            skip = EXCEPTIONS.get(c["name"], set())
            if "runAsNonRoot" not in skip:
                assert _effective(spec, c, "runAsNonRoot") is True, (name, c["name"])
                uid = _effective(spec, c, "runAsUser")
                assert uid is not None and uid != 0, (name, c["name"])
            if "readOnlyRootFilesystem" not in skip:
                assert c["securityContext"]["readOnlyRootFilesystem"] is True, (name, c["name"])

    def test_the_exceptions_are_only_the_model_server(self, full):
        rel = []
        for name, spec, c in containers(full):
            sc = c["securityContext"]
            if sc["readOnlyRootFilesystem"] is not True or _effective(spec, c, "runAsNonRoot") is not True:
                rel.append(c["name"])
        assert set(rel) <= set(EXCEPTIONS)

    def test_every_container_has_requests_and_limits(self, full):
        for name, spec, c in containers(full):
            r = c["resources"]
            assert r.get("requests") and r.get("limits"), (name, c["name"])

    def test_writable_paths_of_the_read_only_runtime_are_explicit_mounts(self, full):
        _, spec, c = _runtime(full)
        mounts = {m["mountPath"]: m for m in c["volumeMounts"]}
        vols = {v["name"]: v for v in spec["volumes"]}
        for path in ("/tmp", "/data", "/app/.agentic-os"):
            assert path in mounts, path
            assert "emptyDir" in vols[mounts[path]["name"]], path
            assert not mounts[path].get("readOnly")
        env = {e["name"]: e.get("value") for e in c["env"]}
        assert env["AGENTIC_OS_HOME"] == "/data" and env["HOME"] == "/tmp"
        assert vols[mounts["/etc/redevops"]["name"]].get("configMap")
        assert mounts["/etc/redevops"].get("readOnly") is True

    def test_runtime_has_startup_liveness_and_readiness_probes_on_health(self, full):
        _, _, c = _runtime(full)
        for p in ("startupProbe", "livenessProbe", "readinessProbe"):
            assert c[p]["httpGet"]["path"] == "/health" and c[p]["httpGet"]["port"] == "http", p
        assert c["startupProbe"]["failureThreshold"] >= 6

    def test_other_long_running_services_have_readiness_probes(self, full):
        for name, spec in pod_specs(full):
            if name.endswith("-verify"):
                continue
            for c in spec["containers"]:
                assert "readinessProbe" in c, (name, c["name"])

    def test_the_services_are_cluster_ip_and_there_is_no_ingress(self, full):
        for s in kinds(full, "Service"):
            assert s["spec"].get("type", "ClusterIP") == "ClusterIP", s["metadata"]["name"]
            assert not any("nodePort" in p for p in s["spec"]["ports"])
        assert not kinds(full, "Ingress") and not kinds(full, "Gateway") and not kinds(full, "HTTPRoute")

    def test_a_networkpolicy_restricts_the_runtime_ingress_and_egress_by_default(self):
        docs = render()
        nps = kinds(docs, "NetworkPolicy")
        assert nps
        rt = [n for n in nps if n["spec"]["podSelector"]["matchLabels"].get("app.kubernetes.io/component") == "runtime"][0]
        assert set(rt["spec"]["policyTypes"]) == {"Ingress", "Egress"}
        for rule in rt["spec"]["ingress"]:
            assert rule.get("from"), "an ingress rule without peers would allow everyone"
            for peer in rule["from"]:
                assert peer != {} and "ipBlock" not in peer
        text = yaml.safe_dump(rt)
        assert "0.0.0.0/0" not in text
        assert any(p.get("port") == 53 for r in rt["spec"]["egress"] for p in r.get("ports", [])), "DNS must stay reachable"

    def test_bundled_pgvector_accepts_traffic_only_from_the_runtime(self, full):
        np = [n for n in kinds(full, "NetworkPolicy") if n["spec"]["podSelector"]["matchLabels"]["app.kubernetes.io/component"] == "pgvector"][0]
        assert np["spec"]["ingress"][0]["from"][0]["podSelector"]["matchLabels"]["app.kubernetes.io/component"] == "runtime"
        assert np["spec"]["egress"] == []

    def test_networkpolicy_can_be_switched_off_explicitly(self):
        assert not kinds(render(["networkPolicy.enabled=false"]), "NetworkPolicy")

    def test_extra_ingress_peers_and_egress_rules_are_rendered_only_when_given(self):
        docs = render(["networkPolicy.ingress.from[0].namespaceSelector.matchLabels.team=edge",
                       "networkPolicy.egress.extraRules[0].to[0].ipBlock.cidr=198.51.100.0/24"])
        rt = [n for n in kinds(docs, "NetworkPolicy") if n["spec"]["podSelector"]["matchLabels"]["app.kubernetes.io/component"] == "runtime"][0]
        assert {"namespaceSelector": {"matchLabels": {"team": "edge"}}} in [p for r in rt["spec"]["ingress"] for p in r["from"]]
        assert any(t.get("ipBlock", {}).get("cidr") == "198.51.100.0/24" for r in rt["spec"]["egress"] for t in r.get("to", []))

    def test_control_token_is_read_from_a_secret_reference_only(self, full):
        _, _, c = _runtime(full)
        tok = [e for e in c["env"] if e["name"] == "AGENTIC_OS_CONTROL_TOKEN"][0]
        assert tok["valueFrom"]["secretKeyRef"] == {"name": "ctl", "key": "AGENTIC_OS_CONTROL_TOKEN"} and "value" not in tok
        assert not [e for e in _runtime(render())[2]["env"] if e["name"] == "AGENTIC_OS_CONTROL_TOKEN"]

    def test_no_secret_like_env_var_carries_a_literal_value(self, full):
        for name, spec, c in containers(full):
            for e in c.get("env", []):
                if re.search(r"(TOKEN|KEY|PASSWORD|SECRET)", e["name"]) and e["name"] != "PERMISSIONS_DIR":
                    assert "value" not in e and "secretKeyRef" in e.get("valueFrom", {}), (name, e["name"])

    def test_the_rendered_manifest_contains_no_literal_default_password(self):
        out = _run(["retriever.pgvector.bundled.enabled=true", "retriever.pgvector.bundled.passwordSecret=pg"]).stdout
        assert "redevops:redevops@" not in out and not re.search(r"POSTGRES_PASSWORD\s*\n?\s*value:", out)

    def test_bundled_pgvector_needs_an_existing_password_secret(self):
        err = render_fails(["retriever.pgvector.bundled.enabled=true"])
        assert "passwordSecret" in err

    def test_bundled_pgvector_is_off_by_default(self):
        assert not [d for d in render() if d["metadata"]["name"].endswith("-pgvector")]

    def test_optional_workloads_without_an_image_fail_to_render(self):
        assert "cockpit.image" in render_fails(["cockpit.enabled=true"])
        assert "model.server.image" in render_fails(["model.server.enabled=true"])
        assert "security.image" in render_fails(["security.enabled=true", "security.image.repository=ghcr.io/x/enterprise"])

    @pytest.mark.parametrize("img", ["rocm/vllm:latest", "rocm/vllm@sha256:REPLACE_WITH_PINNED_DIGEST", "rocm/vllm:v0.6.0"])
    def test_model_server_image_must_be_a_real_digest(self, img):
        assert "model.server.image" in render_fails(["model.server.enabled=true", f"model.server.image={img}"])

    def test_the_verify_hook_runs_the_runtime_image_hardened(self):
        job = kinds(render(), "Job")[0]
        spec = job["spec"]["template"]["spec"]
        assert spec["containers"][0]["image"].endswith("@" + DIGEST)
        assert spec["automountServiceAccountToken"] is False and spec["restartPolicy"] == "Never"

    def test_image_pull_secrets_are_supported_for_private_packages(self):
        _, spec, _ = _runtime(render(["imagePullSecrets[0].name=ghcr-staged"]))
        assert spec["imagePullSecrets"] == [{"name": "ghcr-staged"}]
        job = kinds(render(["imagePullSecrets[0].name=ghcr-staged"]), "Job")[0]
        assert job["spec"]["template"]["spec"]["imagePullSecrets"] == [{"name": "ghcr-staged"}]

    def test_hardening_is_overridable_only_explicitly(self):
        _, spec, c = _runtime(render(["runtime.securityContext.readOnlyRootFilesystem=false"]))
        assert c["securityContext"]["readOnlyRootFilesystem"] is False
        assert c["securityContext"]["capabilities"]["drop"] == ["ALL"]          # the rest of the defaults still apply
        _, _, c = _runtime(render())
        assert c["securityContext"]["readOnlyRootFilesystem"] is True

    def test_service_type_is_only_exposed_by_an_explicit_value(self):
        svc = [s for s in kinds(render(), "Service") if s["metadata"]["name"] == "rel-redevops-runtime"][0]
        assert svc["spec"]["type"] == "ClusterIP"
        svc = [s for s in kinds(render(["runtime.service.type=NodePort"]), "Service") if s["metadata"]["name"] == "rel-redevops-runtime"][0]
        assert svc["spec"]["type"] == "NodePort"

    def test_enterprise_image_requires_an_immutable_reference_too(self):
        docs = render(["security.enabled=true", "security.image.repository=ghcr.io/redevops-io/redevops-runtime-enterprise",
                       "security.image.digest=" + OTHER_DIGEST])
        assert _runtime(docs)[2]["image"] == "ghcr.io/redevops-io/redevops-runtime-enterprise@" + OTHER_DIGEST

    def test_helm_lint_passes_with_the_required_values(self):
        cmd = [HELM, "lint", str(CHART)]
        for s in _base_sets():
            cmd += ["--set", s]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/tmp"})
        assert p.returncode == 0, p.stdout + p.stderr

    def test_the_default_render_has_no_latest_no_host_mounts_no_privileged(self):
        out = _run().stdout
        assert not re.search(r"image:\s*\S+:latest", out)
        for bad in ("hostPath", "hostNetwork: true", "privileged: true", "docker.sock", "LoadBalancer", "NodePort"):
            assert bad not in out, bad
