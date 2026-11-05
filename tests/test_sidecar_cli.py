"""RC3: the sidecar CLI is fail-closed (packaging/sidecar_main.py, packaging/check-sidecar-cli.py).

Incident (RC2 and v0.3.2): `redevops-sidecar --help` was treated as `serve`: it started a server, bound a port and printed a one-time
launch URL. Now only `serve [--exit-on-stdin-eof]`, `bootstrap` and `help|-h|--help` do anything; no argument and every unknown
command or option print the usage on stderr (ASCII) and exit 2 without starting a server, binding a port, creating a launch code or
touching the filesystem. The frozen-binary equivalent of this file is `packaging/check-sidecar-cli.py <binary>`.
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ENTRY = REPO / "packaging" / "sidecar_main.py"
CHECK = REPO / "packaging" / "check-sidecar-cli.py"
SRC_RS = REPO / "deploy" / "launcher" / "src-tauri" / "src"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


main_mod = _load(ENTRY, "_sidecar_main_cli")
check = _load(CHECK, "_check_sidecar_cli")


@pytest.mark.parametrize("argv,action", [
    ([], "error"), (["--help"], "help"), (["-h"], "help"), (["help"], "help"), (["serve", "-h"], "help"), (["bootstrap", "--help"], "help"),
    (["serve"], "serve"), (["serve", "--exit-on-stdin-eof"], "serve"), (["bootstrap"], "bootstrap"),
    (["--version"], "error"), (["--bogus"], "error"), (["frobnicate"], "error"), (["SERVE"], "error"), ([""], "error"),
    (["serve", "--bogus"], "error"), (["serve", "x"], "error"), (["serve", "--exit-on-stdin-eof", "--bogus"], "error"),
    (["serve", "--bogus", "--help"], "error"), (["bootstrap", "--x"], "error"), (["--exit-on-stdin-eof"], "error"),
    (["--exit-on-stdin-eof", "serve"], "error"),
])
def test_parse_args(argv, action):
    assert main_mod.parse_args(argv)[0] == action


def test_usage_is_ascii_and_names_every_command():
    main_mod.USAGE.encode("ascii")
    for word in ("serve", "bootstrap", "--exit-on-stdin-eof", "help", "exits 2"):
        assert word in main_mod.USAGE


def _launcher_invocations():
    """Every argument vector the repository's launcher, scripts and acceptance kits pass to the sidecar."""
    found = []
    for f in list(SRC_RS.glob("*.rs")) + [REPO / "packaging/acceptance/macos-r0prime/gui/driver/src/main.rs"]:
        t = f.read_text(encoding="utf-8")
        for m in re.finditer(r'\.args?\(\[?((?:"[^"]*",?\s*)+)\]?\)', t):
            vals = re.findall(r'"([^"]*)"', m.group(1))
            if vals and vals[0] in ("serve", "bootstrap"):
                found.append(vals)
    for f in (REPO / "packaging/check-sidecar-lifecycle.py", REPO / "packaging/candidate/check_app_smoke.py"):
        for m in re.finditer(r'\[(?:str\(binary\)|sidecar|\w+), ?"(serve|bootstrap)"((?:, ?"[^"]*")*)\]', f.read_text(encoding="utf-8")):
            found.append([m.group(1)] + re.findall(r'"([^"]*)"', m.group(2)))
    return found


def test_every_documented_launcher_invocation_is_still_accepted():
    inv = _launcher_invocations()
    assert ["serve", "--exit-on-stdin-eof"] in inv and ["bootstrap"] in inv and ["serve"] in inv, inv
    for argv in inv:
        assert main_mod.parse_args(argv)[0] in ("serve", "bootstrap"), argv


def test_shell_and_powershell_callers_pass_an_explicit_command():
    for f in ("packaging/acceptance-ubuntu.sh", "packaging/check-sidecar-locales.sh"):
        t = (REPO / f).read_text(encoding="utf-8")
        assert re.search(r'"\$(SIDE|BIN)" (serve|bootstrap)', t), f
    assert "-ArgumentList @('serve')" in (REPO / "packaging/acceptance/windows-r0prime/Common.ps1").read_text(encoding="utf-8")


def test_the_check_script_passes_against_the_source_entry(capsys):
    assert check.main(["--python-entry", str(ENTRY)]) == 0
    assert "fail-closed" in capsys.readouterr().out


def test_incident_help_starts_no_server_prints_no_launch_code_and_creates_nothing(tmp_path):
    port = 0
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "USERPROFILE": str(tmp_path), "PYTHONPATH": str(REPO),
           "PROJECTS_PORT": str(port), "PROJECTS_HOST": "127.0.0.1"}
    before = sorted(os.listdir(tmp_path))
    p = subprocess.run([sys.executable, str(ENTRY), "--help"], env=env, cwd=tmp_path, stdin=subprocess.DEVNULL, capture_output=True,
                       timeout=30)
    assert p.returncode == 0 and b"usage:" in p.stdout and p.stderr == b""
    assert b"#lc=" not in p.stdout + p.stderr and b"launch" not in (p.stdout + p.stderr).lower().replace(b"launcher", b"")
    assert sorted(os.listdir(tmp_path)) == before
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=0.3)


@pytest.mark.parametrize("enc", ["cp1252", "latin-1", "ascii", "cp437"])
@pytest.mark.parametrize("args,rc", [(["--help"], 0), ([], 2), (["--bogus"], 2)])
def test_usage_is_safe_in_a_legacy_console_encoding(tmp_path, enc, args, rc):
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "PYTHONPATH": str(REPO), "PYTHONIOENCODING": enc, "PYTHONUTF8": "0"}
    p = subprocess.run([sys.executable, str(ENTRY), *args], env=env, cwd=tmp_path, stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
    assert p.returncode == rc and b"UnicodeEncodeError" not in p.stderr and b"Traceback" not in p.stderr
    (p.stdout + p.stderr).decode("ascii")


def test_encoding_is_configured_before_anything_is_printed():
    src = ENTRY.read_text(encoding="utf-8")
    body = src[src.index("def main("):]
    assert body.index("_utf8_stdio()") < body.index("parse_args(") < body.index("sys.stdout.write")


def test_spec_entry_and_about_record_are_embedded():
    spec = (REPO / "packaging" / "redevops-sidecar.spec").read_text(encoding="utf-8")
    assert '["sidecar_main.py"]' in spec and '"about.json"' in spec
    assert "agentic_os" in spec and 'collect_data_files("agentic_os")' in spec       # ships agentic_os/about_assets/** too
    assert "about_assets" not in spec                                                 # nothing special: it is package data
    assert any((REPO / "agentic_os" / "about_assets" / n).is_file() for n in ("about.html", "app.js", "app.css"))
