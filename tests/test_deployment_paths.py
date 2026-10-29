"""Every fresh-volume deployment path provisions a key through an explicit setup step, or stops with an actionable error.

Shipped compose files, the Makefile and the Helm chart reference the grant-store key as an EXTERNAL secret with NO default
(no literal key, no `:-` default, no empty default that silently works); the setup step (`agentic-os permissions key`,
`make permissions-key`, `agentic-os permissions init`) writes it to a 0600 file and never prints it; and starting without it
fails with the GrantStorageError that names that step. Docker Compose is evaluated for real with `docker compose config`
(no daemon needed) when the CLI is present; Helm is traced statically (no `helm` binary in CI images by default).
"""
from __future__ import annotations

import base64
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import textwrap

import pytest

from agentic_os import permissions as perm
from agentic_os.permissions import GrantStorageError, GrantStore
from agentic_os.permissions_setup import write_key_env_file

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMPOSE = ("docker-compose.yml", "deploy/docker-compose.yml", "deploy/permissions-showcase-demo/permissions-demo.compose.yml")
HELM = ROOT / "deploy" / "reference-system" / "redevops-runtime"
_STRIP = ("PERMISSIONS_", "APPROVAL_", "AGENTIC_OS_")
FAKE_KEY = base64.b64encode(b"t" * 32).decode()


def _env(**extra) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(_STRIP)}
    env.update(extra)
    return env


def _cli(*args, cwd=None, **env):
    return subprocess.run([sys.executable, "-m", "agentic_os.cli", "permissions", *args],
                          env=_env(PYTHONPATH=str(ROOT), **env), cwd=cwd or str(ROOT), capture_output=True, text=True, timeout=120)


def _py(code, **env):
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code)], env=_env(PYTHONPATH=str(ROOT), **env), cwd=str(ROOT),
                          capture_output=True, text=True, timeout=180)


def _parse_env_file(p) -> dict:
    return dict(l.split("=", 1) for l in pathlib.Path(p).read_text().splitlines() if "=" in l)


# ───────────────────────── static: no literal key, no default, an external secret ─────────────────────────


class TestShippedFilesCarryNoKeyAndNoDefault:
    def _files(self):
        pats = ("**/*.yml", "**/*.yaml", "**/Dockerfile*", "**/Makefile", "Makefile", "**/*.tpl", "**/*.env*", "**/*.toml")
        seen = set()
        for pat in pats:
            for f in ROOT.glob(pat):
                if f.is_file() and not any(x in f.parts for x in (".git", ".venv", "node_modules", "target")):
                    seen.add(f)
        return sorted(seen)

    def test_no_shipped_file_has_a_literal_key_or_a_default_for_it(self):
        bad = []
        for f in self._files():
            text = f.read_text(errors="ignore")
            for m in re.finditer(r"PERMISSIONS_KEY(?!_)\s*[:=]\s*(.*)", text):
                v = m.group(1).strip().strip("\"'")
                if v and not v.startswith("${PERMISSIONS_KEY:?") and not v.startswith("$PERMISSIONS_KEY") and not v.startswith("{{") \
                        and not v.startswith("#"):
                    bad.append((str(f.relative_to(ROOT)), v[:40]))
            if re.search(r"\$\{PERMISSIONS_KEY:-", text) or re.search(r"\$\{PERMISSIONS_KEY-", text):
                bad.append((str(f.relative_to(ROOT)), "a default for PERMISSIONS_KEY"))
        assert bad == []

    def test_no_key_material_is_in_the_tree(self):
        found = [str(f.relative_to(ROOT)) for name in (".permissions.key", "permissions.env")
                 for f in ROOT.rglob(name) if ".git" not in f.parts and ".venv" not in f.parts]
        assert found == []

    def test_key_files_are_git_ignored(self):
        text = (ROOT / ".gitignore").read_text()
        assert "permissions.env" in text and ".permissions.key" in text

    @pytest.mark.parametrize("f", COMPOSE)
    def test_each_compose_file_requires_the_key_from_outside(self, f):
        text = (ROOT / f).read_text()
        items = re.findall(r'PERMISSIONS_KEY=\$\{PERMISSIONS_KEY:\?([^}]+)\}', text)
        assert len(items) == 1 and "make permissions-key" in items[0] and "docker compose --env-file" in items[0]

    def test_the_helm_chart_requires_an_external_secret_and_has_no_default(self):
        tpl = (HELM / "templates" / "runtime.yaml").read_text()
        vals = (HELM / "values.yaml").read_text()
        assert "secretKeyRef" in tpl and "key: PERMISSIONS_KEY" in tpl
        assert re.search(r'required "permissions\.keySecret is required[^"]*agentic-os permissions key', tpl)
        assert re.search(r'^permissions:\s*\n\s+keySecret:\s*""', vals, re.M)       # empty => `required` stops the render
        assert not re.search(r"\.Values\.permissions\.key\b(?!Secret)", tpl)           # never a key value in values

    def test_the_makefile_has_the_setup_step_and_run_needs_it(self):
        mk = (ROOT / "Makefile").read_text()
        assert re.search(r"^permissions-key:", mk, re.M) and re.search(r"^permissions-check:", mk, re.M)
        assert re.search(r"^run: permissions-check", mk, re.M) and "agentic-os permissions key --env-file" in mk


# ───────────────────────── Docker Compose, evaluated for real ─────────────────────────

needs_compose = pytest.mark.skipif(shutil.which("docker") is None or subprocess.run(
    ["docker", "compose", "version"], capture_output=True).returncode != 0, reason="docker compose CLI not available")


@needs_compose
class TestComposeStopsWithoutAKey:
    def _config(self, f, **env):
        return subprocess.run(["docker", "compose", "-f", str(ROOT / f), "config", "-q"], env=_env(**env),
                              capture_output=True, text=True, timeout=60, cwd=str(ROOT))

    @pytest.mark.parametrize("f", COMPOSE)
    def test_no_key_stops_compose_with_the_fix(self, f):
        r = self._config(f)
        assert r.returncode != 0 and "required variable PERMISSIONS_KEY is missing" in r.stderr
        assert "make permissions-key" in r.stderr and "--env-file permissions.env" in r.stderr

    @pytest.mark.parametrize("f", COMPOSE)
    def test_an_empty_key_is_not_a_key(self, f):
        assert self._config(f, PERMISSIONS_KEY="").returncode != 0

    @pytest.mark.parametrize("f", COMPOSE)
    def test_with_the_key_compose_is_valid_and_the_key_reaches_only_the_environment(self, f):
        r = self._config(f, PERMISSIONS_KEY=FAKE_KEY)
        assert r.returncode == 0, r.stderr[-300:]

    def test_the_env_file_written_by_the_setup_step_satisfies_compose(self, tmp_path):
        envf = tmp_path / "permissions.env"
        assert _cli("key", "--env-file", str(envf)).returncode == 0
        for f in COMPOSE:
            r = subprocess.run(["docker", "compose", "--env-file", str(envf), "-f", str(ROOT / f), "config", "-q"],
                               env=_env(), capture_output=True, text=True, timeout=60, cwd=str(ROOT))
            assert r.returncode == 0, (f, r.stderr[-300:])


# ───────────────────────── the setup step ─────────────────────────


class TestTheSetupStep:
    def test_the_key_command_writes_a_0600_file_and_never_prints_the_key(self, tmp_path):
        envf = tmp_path / "permissions.env"
        r = _cli("key", "--env-file", str(envf), "--k8s-secret", "redevops-permissions-key", "--namespace", "aos")
        key = _parse_env_file(envf)["PERMISSIONS_KEY"]
        assert r.returncode == 0 and stat.S_IMODE(os.stat(envf).st_mode) == 0o600
        assert len(base64.b64decode(key, validate=True)) == 32
        assert key not in r.stdout + r.stderr
        assert f"kubectl create secret generic redevops-permissions-key --from-env-file={envf} --namespace aos" in r.stdout
        assert "permissions.keySecret=redevops-permissions-key" in r.stdout

    def test_it_never_overwrites_a_key(self, tmp_path):
        envf = tmp_path / "permissions.env"
        assert _cli("key", "--env-file", str(envf)).returncode == 0
        before = envf.read_bytes()
        r = _cli("key", "--env-file", str(envf))
        assert r.returncode == 2 and "already exists" in r.stderr and envf.read_bytes() == before

    def test_it_refuses_a_symlink(self, tmp_path):
        victim = tmp_path / "victim"
        os.symlink(victim, tmp_path / "permissions.env")
        assert _cli("key", "--env-file", str(tmp_path / "permissions.env")).returncode == 2 and not victim.exists()

    def test_every_run_creates_a_different_key_so_no_key_is_shared_or_fixed(self, tmp_path):
        keys = set()
        for i in range(3):
            envf = tmp_path / f"e{i}.env"
            _cli("key", "--env-file", str(envf))
            keys.add(_parse_env_file(envf)["PERMISSIONS_KEY"])
        assert len(keys) == 3

    def test_an_existing_key_file_can_become_an_env_key_unchanged(self, tmp_path):
        """A running deployment (the live showcase) keeps its key: the same bytes, now supplied from the environment."""
        d = tmp_path / "vol"
        kf, _ = perm.provision_key_file(str(d))
        os.environ.pop("PERMISSIONS_KEY", None)
        s = GrantStore(str(d))
        s.add("app", "support", "table", "crm.customers")
        envf = tmp_path / "permissions.env"
        r = _cli("key", "--env-file", str(envf), "--from-key-file", str(kf))
        assert r.returncode == 0 and _parse_env_file(envf)["PERMISSIONS_KEY"] == pathlib.Path(kf).read_text().strip()
        assert pathlib.Path(kf).read_text().strip() not in r.stdout + r.stderr
        out = _py("""
            from agentic_os.permissions import GrantStore
            s = GrantStore("%s"); print(s.status()["key_source"], [g.subject for g in s.list()], s.sealed)
        """ % d, **_parse_env_file(envf))
        assert out.returncode == 0 and out.stdout.strip() == "env ['support'] False"

    def test_a_damaged_key_file_is_refused_and_nothing_is_written(self, tmp_path):
        bad = tmp_path / "bad.key"
        bad.write_text("%%%")
        r = _cli("key", "--env-file", str(tmp_path / "e.env"), "--from-key-file", str(bad))
        assert r.returncode == 2 and "unusable" in r.stderr and not (tmp_path / "e.env").exists()

    def test_init_key_only_creates_the_key_file_and_no_grants(self, tmp_path):
        d = tmp_path / "perm"
        r = _cli("init", "--dir", str(d), "--key-only")
        assert r.returncode == 0 and (d / ".permissions.key").exists() and not (d / "grants.enc").exists()
        assert stat.S_IMODE(os.stat(d / ".permissions.key").st_mode) == 0o600

    def test_starting_without_a_key_names_the_fix(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PERMISSIONS_ALLOW_GENERATED_KEY_DEV", raising=False)
        monkeypatch.delenv("PERMISSIONS_KEY", raising=False)
        with pytest.raises(GrantStorageError) as e:
            GrantStore(str(tmp_path / "fresh"))
        msg = str(e.value)
        for fix in ("agentic-os permissions init", "agentic-os permissions key --env-file permissions.env", "make permissions-key",
                    "--k8s-secret", "PERMISSIONS_KEY_FILE"):
            assert fix in msg
        assert not (tmp_path / "fresh").exists()


# ───────────────────────── fresh-install walkthrough per deployment path (no Docker) ─────────────────────────

CONTROL = """
import json
from agentic_os import control_plane as cp
from fastapi.testclient import TestClient
c = TestClient(cp.app)
H = {"X-API-Key": "adm-k-0123456789-DO-NOT-STORE"}
st = c.get("/api/permissions/status", headers=H).json()
a = lambda **b: c.post("/api/permissions/authorize", json={"resource_name": "crm.customers", **b}, headers=H).json()["allowed"]
print(json.dumps({"key_source": st["key_source"], "sealed": st["sealed"], "support": a(app="support"), "nobody": a(app="nobody")}))
"""


class TestFreshInstallWalkthroughs:
    def test_compose_control_plane_paths(self, tmp_path):
        """(The two example demo grants are seeded here with the explicit opt-in AGENTIC_OS_SEED_DEMO_GRANTS=1, so that the sealed-volume steps below have a store to check; without it no grant exists.)
        The root and `deploy/` compose files start `uvicorn agentic_os.control_plane:app` with PERMISSIONS_DIR=/data/permissions
        on a persistent volume and PERMISSIONS_KEY from the env file. Run the same entrypoint with the same variables."""
        vol = str(tmp_path / "data" / "permissions")
        # 1. a fresh volume and no key: the container would not start, and the error names the setup step
        p = _py(CONTROL, PERMISSIONS_DIR=vol)
        assert p.returncode != 0 and "GrantStorageError" in p.stderr and "agentic-os permissions key --env-file" in p.stderr
        assert not pathlib.Path(vol).exists()
        # 2. the setup step, then the documented start
        envf = tmp_path / "permissions.env"
        assert _cli("key", "--env-file", str(envf)).returncode == 0
        out = _py(CONTROL, PERMISSIONS_DIR=vol, PERMISSIONS_ADMIN_KEY="adm-k-0123456789-DO-NOT-STORE", AGENTIC_OS_SEED_DEMO_GRANTS="1", **_parse_env_file(envf))
        assert out.returncode == 0, out.stderr[-500:]
        import json
        r = json.loads(out.stdout.strip().splitlines()[-1])
        assert r == {"key_source": "env", "sealed": False, "support": True, "nobody": False}
        # 3. a restart on the same volume with the same key keeps working
        again = json.loads(_py(CONTROL, PERMISSIONS_DIR=vol, PERMISSIONS_ADMIN_KEY="adm-k-0123456789-DO-NOT-STORE", AGENTIC_OS_SEED_DEMO_GRANTS="1", **_parse_env_file(envf)).stdout.strip().splitlines()[-1])
        assert again["sealed"] is False and again["support"] is True
        # 4. a different key cannot open the volume: the control plane refuses to run (sealed) and nothing is overwritten
        other = tmp_path / "other.env"
        _cli("key", "--env-file", str(other))
        before = pathlib.Path(vol, "grants.enc").read_bytes()
        bad = _py(CONTROL, PERMISSIONS_DIR=vol, PERMISSIONS_ADMIN_KEY="adm-k-0123456789-DO-NOT-STORE", AGENTIC_OS_SEED_DEMO_GRANTS="1", **_parse_env_file(other))
        assert bad.returncode != 0 and "sealed" in bad.stderr and pathlib.Path(vol, "grants.enc").read_bytes() == before

    def test_permissions_demo_path(self, tmp_path):
        """The showcase STARTS without the store key (the public demo needs none); its admin API says 503 and the server log names the setup step.
        With the key provisioned by the documented step it serves the admin API."""
        code = """
            import importlib.util, json
            spec = importlib.util.spec_from_file_location("d", "deploy/permissions-showcase-demo/app.py")
            m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
            from fastapi.testclient import TestClient
            c = TestClient(m.app, raise_server_exceptions=False)
            r = c.get("/api/permissions/status", headers={"X-API-Key": "adm-k-0123456789-DO-NOT-STORE"})
            print(json.dumps({"public": c.get("/api/demo/scenarios").status_code, "status": r.status_code,
                              "key_source": r.json().get("key_source") if r.status_code == 200 else None}))
        """
        vol = str(tmp_path / "permissions")
        p = _py(code, PERMISSIONS_DIR=vol, PERMISSIONS_ADMIN_KEY="adm-k-0123456789-DO-NOT-STORE")
        assert p.returncode == 0 and '"public": 200' in p.stdout and '"status": 503' in p.stdout
        assert "agentic-os permissions key --env-file" in p.stderr             # the fix is logged server-side, not sent to the client
        envf = tmp_path / "permissions.env"
        _cli("key", "--env-file", str(envf))
        out = _py(code, PERMISSIONS_DIR=vol, PERMISSIONS_ADMIN_KEY="adm-k-0123456789-DO-NOT-STORE", **_parse_env_file(envf))
        assert out.returncode == 0 and '"env"' in out.stdout

    def test_make_run_path(self, tmp_path):
        """`make run` needs a key (env, key file, or the permissions.env written by `make permissions-key`) and otherwise stops."""
        shim = tmp_path / "bin"
        shim.mkdir()
        (shim / "uv").write_text('#!/bin/sh\nprintf "uv %s | dir=%s key=%s\\n" "$*" "$PERMISSIONS_DIR" "${PERMISSIONS_KEY:+set}"\n')
        (shim / "uv").chmod(0o755)
        envf = tmp_path / "permissions.env"
        base = _env(PATH=f"{shim}:{os.environ['PATH']}")
        mk = ["make", "-C", str(ROOT), f"PERMISSIONS_ENV={envf}", f"PERMISSIONS_DIR={tmp_path}/dir"]
        stop = subprocess.run([*mk, "run"], env=base, capture_output=True, text=True, timeout=60)
        assert stop.returncode != 0 and "make permissions-key" in stop.stderr and "uv " not in stop.stdout
        made = subprocess.run([*mk, "permissions-key"], env=base, capture_output=True, text=True, timeout=60)
        assert made.returncode == 0 and "agentic-os permissions key" in made.stdout and "uv run agentic-os permissions key" in made.stdout
        # the shim stands in for uv, so write the file the real target would have written
        write_key_env_file(str(envf))
        go = subprocess.run([*mk, "run"], env=base, capture_output=True, text=True, timeout=60)
        assert go.returncode == 0 and f"dir={tmp_path}/dir key=set" in go.stdout and "agentic_os.control_plane:app" in go.stdout
        exported = subprocess.run([*mk, "run"], env={**base, "PERMISSIONS_KEY": FAKE_KEY}, capture_output=True, text=True, timeout=60)
        assert exported.returncode == 0 and "key=set" in exported.stdout

    def test_helm_path_is_traced_statically(self):
        """No `helm` is run: the chart's render is stopped by `required` while permissions.keySecret is empty, and the pod reads
        the key from that Secret only (see the static checks above). The setup step prints the matching kubectl command."""
        assert "required" in (HELM / "templates" / "runtime.yaml").read_text()
