"""The first-party wheel list is derived from packaging/git-pins.json, never typed into a script (D-NEW-1).

0.6.0-rc1 could not build: git-pins.json grew a fifth first-party wheel (discovery-runtime) while three build scripts
kept a hand-written list of four, so `pip check` failed ("agentic-os requires discovery-runtime"). These tests fail when
the pins and the scripts (or the Python tools that name the first-party set) disagree. Offline, no build.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PK = ROOT / "packaging"
sys.path.insert(0, str(PK / "tools"))

import first_party as fp  # noqa: E402

SCRIPTS = ["build-wheelhouse.sh", "build-sidecar.sh", "build-notices.sh"]
PY_USERS = ["sbom/analyze.py", "sbom/generate_source_offer.py", "tools/collect_source_refs.py", "tools/compare_builds.py"]


def pin_names() -> set[str]:
    return {fp.norm(d["name"]) for d in json.loads((PK / "git-pins.json").read_text())["dependencies"]}


def lock_names() -> set[str]:
    return fp.lock_names(PK / "locks" / "requirements-linux.lock")


def make_wheelhouse(tmp: Path, first: set[str], third: set[str]) -> Path:
    wh = tmp / "wheelhouse"
    wh.mkdir()
    for n in sorted(first | third):
        (wh / f"{n.replace('-', '_')}-1.0-py3-none-any.whl").write_bytes(b"x")
    return wh


# ---- the source of truth -------------------------------------------------------------------------------------------

def test_first_party_names_are_the_pins_plus_this_checkout():
    names = fp.first_party_names()
    assert names == pin_names() | {"agentic-os"}
    assert "discovery-runtime" in names and len(names) == 5


def test_lock_names_are_normalised_distribution_names():
    names = lock_names()
    assert len(names) == 21 and "idna" in names and "agentic-os" not in names
    assert not names & fp.first_party_names()


# ---- the scripts do not carry a list ---------------------------------------------------------------------------------

@pytest.mark.parametrize("script", SCRIPTS)
def test_script_has_no_hand_written_first_party_wheel_glob(script):
    text = (PK / script).read_text()
    # any `<first_party_name>-*.whl` glob (agentic_os-*.whl, runtime_contracts-*.whl, ...) is a hand-written list
    for n in sorted(fp.first_party_names() | {"discovery-runtime"}):
        assert f"{n.replace('-', '_')}-*.whl" not in text, f"{script} still names {n} by hand"
    assert not re.search(r"""ls\s+"?\$WH"?/\w+-\*\.whl""", text), f"{script} lists wheels by hand"


@pytest.mark.parametrize("script", SCRIPTS)
def test_script_derives_the_list_from_the_one_source(script):
    text = (PK / script).read_text()
    assert "first_party_wheels" in text
    assert "FIRST=" not in text or "first_party_wheels" in text


def test_common_sh_helper_calls_the_tool_and_stops_on_failure():
    text = (PK / "common.sh").read_text()
    assert "first_party_wheels()" in text
    body = text.split("first_party_wheels()", 1)[1]
    assert "tools/first_party.py" in body and "exit 1" in body


def test_build_wheelhouse_reports_the_expected_count_from_the_tool_not_a_literal():
    text = (PK / "build-wheelhouse.sh").read_text()
    assert "expected 25" not in text and "(4)" not in text
    assert "first_party.py" in text and "summary" in text


@pytest.mark.parametrize("rel", PY_USERS)
def test_python_tools_take_the_first_party_set_from_the_same_source(rel):
    text = (PK / rel).read_text()
    assert "first_party" in text, rel
    assert '"knowledge-frontier", "redevops-connectors"}' not in text and "knowledge_frontier\", \"redevops_connectors\")" not in text


def test_analyze_and_offer_and_refs_sets_equal_the_pins():
    import importlib.util

    for rel in ("tools/collect_source_refs.py", "sbom/generate_source_offer.py"):
        spec = importlib.util.spec_from_file_location("m_" + rel.replace("/", "_").removesuffix(".py"), PK / rel)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        assert set(m.FIRST_PARTY) == fp.first_party_names(), rel
    sys.path.insert(0, str(PK))
    from sbom.analyze import FIRST_PARTY  # noqa: E402
    assert set(FIRST_PARTY) == fp.first_party_names()


# ---- the check itself --------------------------------------------------------------------------------------------------

def test_consistent_wheelhouse_lists_exactly_the_first_party_wheels(tmp_path):
    wh = make_wheelhouse(tmp_path, fp.first_party_names(), lock_names())
    got = fp.select_first_party(sorted(p.name for p in wh.glob("*.whl")), fp.first_party_names(), lock_names())
    assert sorted(fp.dist_of(g) for g in got) == sorted(fp.first_party_names())
    assert len(got) == 5


def test_a_pin_without_a_wheel_is_refused_with_its_name(tmp_path):
    wh = make_wheelhouse(tmp_path, fp.first_party_names() - {"discovery-runtime"}, lock_names())
    with pytest.raises(fp.Inconsistent) as e:
        fp.select_first_party(sorted(p.name for p in wh.glob("*.whl")), fp.first_party_names(), lock_names())
    assert "discovery-runtime" in str(e.value) and "no wheel" in str(e.value)


def test_a_wheel_without_a_pin_is_refused_with_its_name(tmp_path):
    wh = make_wheelhouse(tmp_path, fp.first_party_names() | {"shiny-new-dep"}, lock_names())
    with pytest.raises(fp.Inconsistent) as e:
        fp.select_first_party(sorted(p.name for p in wh.glob("*.whl")), fp.first_party_names(), lock_names())
    assert "shiny-new-dep" in str(e.value) and "no pin" in str(e.value)


def test_a_new_pin_changes_the_derived_list_with_no_script_edit(tmp_path):
    pins = json.loads((PK / "git-pins.json").read_text())
    pins["dependencies"].append({"name": "brand-new-lib"})
    p = tmp_path / "pins.json"
    p.write_text(json.dumps(pins))
    names = fp.first_party_names(p)
    assert "brand-new-lib" in names and len(names) == 6
    wh = make_wheelhouse(tmp_path, fp.first_party_names(), lock_names())     # wheel for the new pin missing
    with pytest.raises(fp.Inconsistent) as e:
        fp.select_first_party(sorted(p2.name for p2 in wh.glob("*.whl")), names, lock_names())
    assert "brand-new-lib" in str(e.value)


def test_a_third_party_wheel_missing_from_the_wheelhouse_is_refused(tmp_path):
    third = lock_names()
    wh = make_wheelhouse(tmp_path, fp.first_party_names(), third - {"idna"})
    with pytest.raises(fp.Inconsistent) as e:
        fp.select_first_party(sorted(p.name for p in wh.glob("*.whl")), fp.first_party_names(), third)
    assert "idna" in str(e.value)


def test_two_wheels_for_one_pin_is_refused(tmp_path):
    wh = make_wheelhouse(tmp_path, fp.first_party_names(), lock_names())
    (wh / "runtime_contracts-9.9-py3-none-any.whl").write_bytes(b"x")
    with pytest.raises(fp.Inconsistent) as e:
        fp.select_first_party(sorted(p.name for p in wh.glob("*.whl")), fp.first_party_names(), lock_names())
    assert "runtime-contracts" in str(e.value)


# ---- the command line the scripts call ---------------------------------------------------------------------------------

def run_tool(*args):
    return subprocess.run([sys.executable, "-I", str(PK / "tools" / "first_party.py"), *args], capture_output=True, text=True)


def test_cli_lists_wheel_file_names_one_per_line(tmp_path):
    wh = make_wheelhouse(tmp_path, fp.first_party_names(), lock_names())
    r = run_tool("wheels", "--wheelhouse", str(wh), "--lock", str(PK / "locks/requirements-linux.lock"))
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert len(lines) == 5 and "discovery_runtime-1.0-py3-none-any.whl" in lines and all("/" not in x for x in lines)


def test_cli_fails_with_one_clear_message_and_no_output_on_inconsistency(tmp_path):
    wh = make_wheelhouse(tmp_path, fp.first_party_names() - {"discovery-runtime"}, lock_names())
    r = run_tool("wheels", "--wheelhouse", str(wh), "--lock", str(PK / "locks/requirements-linux.lock"))
    assert r.returncode == 1 and r.stdout == ""
    assert "discovery-runtime" in r.stderr and "Traceback" not in r.stderr


def test_cli_summary_counts(tmp_path):
    wh = make_wheelhouse(tmp_path, fp.first_party_names(), lock_names())
    r = run_tool("summary", "--wheelhouse", str(wh), "--lock", str(PK / "locks/requirements-linux.lock"))
    assert r.returncode == 0 and r.stdout.strip() == "26 wheels = 21 third-party + 5 first-party"


def test_cli_lock_with_a_missing_file_is_a_clear_error(tmp_path):
    r = run_tool("wheels", "--wheelhouse", str(tmp_path), "--lock", str(tmp_path / "nope.lock"))
    assert r.returncode == 1 and "Traceback" not in r.stderr


# ---- the shell helper, run for real ----------------------------------------------------------------------------------------

def _helper(tmp_path, wh):
    import os
    import shutil
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("no bash")
    if sys.platform == "win32":
        pytest.skip("POSIX shell test")
    # the helper is sourced as the build scripts source it, from a copy of packaging/ so that LOCKS/PK point at the real files
    script = f'. "{PK}/common.sh"; first_party_wheels "{sys.executable}" "{wh}"; printf "%s\\n" "$FIRST"'
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path)}
    return subprocess.run([bash, "-c", "set -euo pipefail; " + script], capture_output=True, text=True, env=env)


def test_shell_helper_sets_FIRST_to_the_five_first_party_wheel_paths(tmp_path):
    import platform
    if platform.system() == "Windows":
        pytest.skip("POSIX shell test")
    lock = {"Darwin": "macos", "Linux": "linux"}[platform.system()]
    wh = make_wheelhouse(tmp_path, fp.first_party_names(), fp.lock_names(PK / "locks" / f"requirements-{lock}.lock"))
    r = _helper(tmp_path, wh)
    assert r.returncode == 0, r.stderr
    paths = r.stdout.split()
    assert len(paths) == 5 and all(p.startswith(str(wh) + "/") for p in paths)
    assert any("/discovery_runtime-" in p for p in paths)


def test_shell_helper_stops_the_calling_script_when_a_pin_has_no_wheel(tmp_path):
    import platform
    if platform.system() == "Windows":
        pytest.skip("POSIX shell test")
    lock = {"Darwin": "macos", "Linux": "linux"}[platform.system()]
    wh = make_wheelhouse(tmp_path, fp.first_party_names() - {"discovery-runtime"}, fp.lock_names(PK / "locks" / f"requirements-{lock}.lock"))
    r = _helper(tmp_path, wh)
    assert r.returncode == 1 and r.stdout == ""
    assert "discovery-runtime" in r.stderr and "no wheel" in r.stderr
