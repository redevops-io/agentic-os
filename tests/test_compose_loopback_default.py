"""Published ports in the compose recipes that ship with the release images default to loopback.

Owner decision 2026-11-06: bind Compose published ports to 127.0.0.1 by default; wider exposure is an explicit opt-in
through ``AOS_BIND_ADDR`` (documented with a warning).  Covered here: the root ``docker-compose.yml`` (the
``redevops-runtime`` image), ``deploy/docker-compose.yml`` (the stand-in fleet demo built from the same Dockerfile),
the ``docker run`` examples in the Dockerfiles and docs, and the compose files the release script generates.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILES = ["docker-compose.yml", "deploy/docker-compose.yml"]
BIND = "${AOS_BIND_ADDR:-127.0.0.1}"
PORT_RE = re.compile(r"^\$\{AOS_BIND_ADDR:-127\.0\.0\.1\}:(\d+):(\d+)$")


def _published(path: str):
    doc = yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))
    for name, svc in (doc.get("services") or {}).items():
        for p in svc.get("ports") or []:
            yield name, p


def _resolve(spec: str, env: dict) -> str:
    """Compose-style ${VAR:-default} interpolation (the only form used in the port specs)."""
    def sub(m):
        return env.get(m.group(1)) or m.group(2)
    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*):-([^}]*)\}", sub, spec)


@pytest.mark.parametrize("path", COMPOSE_FILES)
def test_every_published_port_goes_through_the_bind_address_variable(path):
    ports = list(_published(path))
    assert ports, f"{path} publishes nothing; the test would be vacuous"
    for svc, spec in ports:
        assert isinstance(spec, str) and PORT_RE.match(spec), (path, svc, spec)


@pytest.mark.parametrize("path", COMPOSE_FILES)
def test_unset_variable_resolves_to_loopback(path):
    for svc, spec in _published(path):
        host_ip, host_port, ctr_port = _resolve(spec, {}).split(":")
        assert host_ip == "127.0.0.1", (path, svc, spec)
        assert host_port.isdigit() and ctr_port.isdigit()


@pytest.mark.parametrize("path", COMPOSE_FILES)
def test_explicit_opt_in_widens_it_and_only_it(path):
    for svc, spec in _published(path):
        assert _resolve(spec, {"AOS_BIND_ADDR": "0.0.0.0"}).startswith("0.0.0.0:"), (path, svc, spec)
        assert _resolve(spec, {"AOS_BIND_ADDR": "192.0.2.7"}).startswith("192.0.2.7:"), (path, svc, spec)


@pytest.mark.parametrize("path", COMPOSE_FILES)
def test_no_literal_all_interfaces_binding_or_short_form_port(path):
    text = (ROOT / path).read_text(encoding="utf-8")
    assert "0.0.0.0:" not in re.sub(r"--host 0\.0\.0\.0", "", text)       # the in-container listen address is not a publish
    for line in text.splitlines():
        s = line.strip()
        assert not re.match(r'^ports:\s*\["?\d+:\d+"?\]', s), s            # the old short form "8080:8080"
        assert not re.match(r'^-\s*"?\d+:\d+"?$', s), s
        assert not re.match(r'^-\s*"?\d+"?$', s), s                         # container-port-only publishes on all interfaces


def test_every_compose_file_in_the_release_set_is_covered():
    """A new compose file next to the release images must be added to COMPOSE_FILES (and bound to loopback)."""
    refs = []
    for f in [ROOT / "docker-compose.yml", *sorted((ROOT / "deploy").glob("*.yml")), *sorted((ROOT / "deploy").glob("*.yaml"))]:
        doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        if isinstance(doc, dict) and "services" in doc:
            refs.append(str(f.relative_to(ROOT)))
    assert sorted(refs) == sorted(COMPOSE_FILES), refs


def test_host_network_and_privileged_are_not_used():
    for path in COMPOSE_FILES:
        doc = yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))
        for name, svc in doc["services"].items():
            assert svc.get("network_mode") != "host", (path, name)
            assert not svc.get("privileged"), (path, name)


def test_the_variable_is_documented_with_a_warning():
    for rel in ("docs/CONTROL_PLANE_SECURITY.md", "docs/release/OCI_RELEASE_RUNBOOK.md"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "AOS_BIND_ADDR" in text, rel
        i = text.index("AOS_BIND_ADDR")
        window = text[max(0, i - 600): i + 900].lower()
        assert "0.0.0.0" in window and ("warning" in window or "exposes" in window or "reachable" in window), rel
    for rel in ("docker-compose.yml", "deploy/docker-compose.yml"):
        assert "AOS_BIND_ADDR" in (ROOT / rel).read_text(encoding="utf-8")


# ── docker run examples in the Dockerfiles and the docs ──────────────────────────────────────────────────────
DOC_FILES = ["README.md", "Dockerfile", "Dockerfile.projects", "docs/CONTROL_PLANE_SECURITY.md",
             "docs/release/OCI_RELEASE_RUNBOOK.md", "deploy/README.md"]


@pytest.mark.parametrize("rel", DOC_FILES)
def test_docker_run_examples_publish_on_loopback(rel):
    text = (ROOT / rel).read_text(encoding="utf-8")
    for m in re.finditer(r"(?:docker run|docker create)[^\n`]*?(?:-p|--publish)[ =]([^\s`]+)", text):
        assert m.group(1).startswith(("127.0.0.1:", "[::1]:", "${AOS_BIND_ADDR:-127.0.0.1}:")), (rel, m.group(0))
    for line in text.splitlines():
        if re.search(r"^\s*#?\s*docker run\b.*\s-p\s", line):
            assert re.search(r"-p\s+(127\.0\.0\.1|\[::1\]):", line), (rel, line)


def test_dockerfile_projects_example_is_loopback():
    assert "docker run -p 127.0.0.1:8787:8787 redevops-projects" in (ROOT / "Dockerfile.projects").read_text()


def test_the_release_script_publishes_only_on_loopback():
    sh = (ROOT / "scripts/release/oci_prepare.sh").read_text(encoding="utf-8")
    for m in re.finditer(r'ports:\s*\[\s*"([^"]+)"', sh):
        assert m.group(1).startswith("127.0.0.1:"), m.group(0)
    for m in re.finditer(r"docker run[^\n]*?\s-p\s+\"?([^\s\"]+)", sh):
        assert m.group(1).startswith("127.0.0.1:") or m.group(1).startswith("$"), m.group(0)
    assert re.search(r'-p "127\.0\.0\.1:\$PORT:5000"', sh)             # the throwaway registry
    assert re.search(r'-p "127\.0\.0\.1:\$hp:\$cp"', sh)               # the per-image test containers
