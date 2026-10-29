"""DR-02: the enterprise package (and the engine it needs) is never IMPORTED from ``sys.path`` unless an installed distribution provides it.

A directory named ``context_runtime_enterprise`` (or ``context_runtime`` / ``agent_harness``) placed on ``sys.path`` ahead of the real one, for example the working directory of ``python -m uvicorn``, executes
on import. ``find_spec`` of a top-level name locates it WITHOUT running it, and the located file must be one that an installed distribution's metadata lists (or, for an editable install, lie under the
project directory its ``direct_url.json`` names). Otherwise: nothing requested = treated as absent (the basic default, with a warning); basic requested = the optional engine hook is skipped;
enterprise requested = the app REFUSES to start ("tampered"). In every case the planted module's code never runs: each planted package writes a marker file, which must never exist.
"""
from __future__ import annotations

import importlib
import json
import sys

import pytest

import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from agentic_os import app_gate, tool_actions, trusted_import  # noqa: E402
from agentic_os.permissions_setup import init_local_profile

PLANTED = ("context_runtime_enterprise", "context_runtime", "agent_harness")


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for k in ("AGENTIC_OS_PERMISSIONS", "CR_PERMISSIONS", "CR_IDENTITY_MODE", "AGENTIC_OS_REQUIRE_PERMISSIONS_SETTING", "PERMISSIONS_KEY", "PERMISSIONS_KEY_FILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PERMISSIONS_DIR", str(tmp_path / "perm"))
    for name in [n for n in sys.modules if n.split(".")[0] in PLANTED]:
        monkeypatch.delitem(sys.modules, name)
    # the interpreter's package directories for these tests: the "site" directories the helpers below install into (BB-01: a dist-info anywhere else is never consulted)
    real_dirs = trusted_import._trusted_dirs
    monkeypatch.setattr(trusted_import, "_trusted_dirs", lambda: [*real_dirs(), (tmp_path / "site").resolve(), (tmp_path / "site2").resolve()])
    yield


def plant(tmp_path, monkeypatch, *names):
    """Directories named like the packages, ahead on sys.path, whose import would write a marker file."""
    root = tmp_path / "planted"
    markers = {}
    for name in names:
        pkg = root / name
        pkg.mkdir(parents=True, exist_ok=True)
        marker = tmp_path / f"MARKER-{name}"
        markers[name] = marker
        (pkg / "__init__.py").write_text(f"open({str(marker)!r}, 'w').write('executed')\n")
        (pkg / "apps.py").write_text(f"open({str(marker)!r} + '-apps', 'w').write('executed')\ndef bootstrap(*a, **k):\n    return lambda r=None: None\n")
        (pkg / "fleet_tool_actions.py").write_text(f"open({str(marker)!r} + '-fleet', 'w').write('executed')\ndef fleet_tool_actions(app):\n    return {{'guide.planted': 'read'}}\n")
        (pkg / "tools").mkdir(exist_ok=True)
        (pkg / "tools" / "__init__.py").write_text("")
        (pkg / "tools" / "base.py").write_text(f"open({str(marker)!r} + '-base', 'w').write('executed')\ndef set_default_authorizer(a):\n    pass\n")
    monkeypatch.syspath_prepend(str(root))
    importlib.invalidate_caches()
    return markers


def nothing_ran(tmp_path):
    return sorted(p.name for p in tmp_path.glob("MARKER-*")) == []


def install_dist(tmp_path, monkeypatch, name="drtwopkg", editable_root=None, record=True):
    """A genuinely installed distribution: site-packages with the package and a .dist-info whose RECORD lists its files."""
    site = tmp_path / "site"
    pkg = site / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("VALUE = 1\n")
    info = site / f"{name}-1.0.dist-info"
    info.mkdir(exist_ok=True)
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n")
    (info / "top_level.txt").write_text(name + "\n")
    (info / "RECORD").write_text(f"{name}/__init__.py,,\n" if record else "")
    if editable_root is not None:
        (info / "direct_url.json").write_text(json.dumps({"url": "file://" + str(editable_root), "dir_info": {"editable": True}}))
    monkeypatch.syspath_prepend(str(site))
    importlib.invalidate_caches()
    return site


# ── the primitive ──

def test_a_package_listed_by_an_installed_distribution_is_trusted(tmp_path, monkeypatch):
    install_dist(tmp_path, monkeypatch)
    assert trusted_import.package_status("drtwopkg") == "trusted"


def test_a_planted_directory_ahead_of_the_installed_package_is_untrusted_and_never_executed(tmp_path, monkeypatch):
    install_dist(tmp_path, monkeypatch)
    plant(tmp_path, monkeypatch, "drtwopkg")                                       # prepended later = first on sys.path
    assert trusted_import.package_status("drtwopkg") == "untrusted" and nothing_ran(tmp_path)
    assert trusted_import.import_trusted("drtwopkg") is None and nothing_ran(tmp_path)


def test_a_plain_directory_with_no_distribution_is_untrusted(tmp_path, monkeypatch):
    plant(tmp_path, monkeypatch, "drtwoplain")
    assert trusted_import.package_status("drtwoplain") == "untrusted" and nothing_ran(tmp_path)


def test_a_distribution_that_does_not_list_the_file_does_not_make_the_package_trusted(tmp_path, monkeypatch):
    install_dist(tmp_path, monkeypatch, record=False)
    assert trusted_import.package_status("drtwopkg") == "untrusted"


def test_an_editable_install_is_trusted_only_under_the_project_directory_it_names(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "drtwoedit").mkdir(parents=True)
    (project / "drtwoedit" / "__init__.py").write_text("")
    info = tmp_path / "site2" / "drtwoedit-1.0.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: drtwoedit\nVersion: 1.0\n")
    (info / "top_level.txt").write_text("drtwoedit\n")
    (info / "RECORD").write_text("")
    (info / "direct_url.json").write_text(json.dumps({"url": "file://" + str(project), "dir_info": {"editable": True}}))
    monkeypatch.syspath_prepend(str(tmp_path / "site2"))
    monkeypatch.syspath_prepend(str(project))
    importlib.invalidate_caches()
    assert trusted_import.package_status("drtwoedit") == "trusted"
    plant(tmp_path, monkeypatch, "drtwoedit")                                      # a planted copy outside the project directory
    assert trusted_import.package_status("drtwoedit") == "untrusted" and nothing_ran(tmp_path)


def test_absent_means_not_found_or_blocked(monkeypatch):
    assert trusted_import.package_status("definitely_not_installed_pkg") == "absent"
    monkeypatch.setitem(sys.modules, "blocked_pkg", None)
    assert trusted_import.package_status("blocked_pkg") == "absent"


def test_a_module_injected_into_sys_modules_without_a_distribution_is_untrusted(monkeypatch):
    import types
    monkeypatch.setitem(sys.modules, "injected_pkg", types.ModuleType("injected_pkg"))
    assert trusted_import.package_status("injected_pkg") == "untrusted"


# ── the three situations ──

def test_nothing_requested_a_planted_enterprise_package_is_not_imported(tmp_path, monkeypatch):
    markers = plant(tmp_path, monkeypatch, *PLANTED)
    principal_for = app_gate.install_gate("guide")
    assert nothing_ran(tmp_path), [m.name for m in tmp_path.glob("MARKER-*")]
    assert principal_for() is None and app_gate.gate_for("guide") is None                     # the basic default, as if the package were absent
    assert all(not m.exists() for m in markers.values())


def test_nothing_requested_the_enterprise_package_is_not_imported_even_when_properly_installed(monkeypatch):
    """Design decision: no import of the enterprise package in basic or unconfigured mode, installed or not; only an explicit enterprise request imports it."""
    import _cr_enterprise_stub as stub
    st = stub.install(monkeypatch)
    for mode in ({}, {"AGENTIC_OS_PERMISSIONS": "off"}):
        for k, v in mode.items():
            monkeypatch.setenv(k, v)
        assert app_gate.install_gate("guide")() is None
    assert st.bootstrap_calls == []
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "1")
    app_gate.install_gate("guide")
    assert st.bootstrap_calls == []                                                            # basic: the kernel's own plane, no enterprise bootstrap
    monkeypatch.delenv("AGENTIC_OS_PERMISSIONS")
    monkeypatch.setenv("CR_PERMISSIONS", "1")
    app_gate.install_gate("guide")
    assert len(st.bootstrap_calls) == 1                                                        # an explicit enterprise capability does import and bootstrap it


def test_basic_requested_a_planted_engine_package_is_not_imported(tmp_path, monkeypatch):
    plant(tmp_path, monkeypatch, *PLANTED)
    init_local_profile(str(tmp_path / "perm"))
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "1")
    app_gate.install_gate("guide")
    assert app_gate.gate_for("guide") is not None and nothing_ran(tmp_path)
    assert app_gate.tool_denial("guide", "guide.retrieve") is None                            # the basic gate works without any engine
    assert app_gate.default_authorizer() is app_gate._PROCESS_AUTHORIZER and nothing_ran(tmp_path)


@pytest.mark.parametrize("which", [("context_runtime_enterprise",), ("context_runtime",), ("agent_harness",), PLANTED])
def test_enterprise_requested_a_planted_package_refuses_to_start_and_is_never_executed(tmp_path, monkeypatch, which):
    plant(tmp_path, monkeypatch, *which)
    monkeypatch.setenv("CR_PERMISSIONS", "1")
    with pytest.raises(app_gate.GateStartupError) as e:
        app_gate.install_gate("guide")
    assert not isinstance(e.value, app_gate.EnterpriseCapabilityMissing)                      # not "missing": present, and not to be trusted
    assert "not provided by an installed distribution" in str(e.value) and nothing_ran(tmp_path)
    assert "guide" not in app_gate._ENTERPRISE and app_gate.gate_for("guide") is None


def test_enterprise_requested_and_nothing_installed_is_still_the_missing_capability_error(monkeypatch):
    monkeypatch.setenv("CR_PERMISSIONS", "1")
    for name in PLANTED:
        monkeypatch.setitem(sys.modules, name, None)
    with pytest.raises(app_gate.EnterpriseCapabilityMissing):
        app_gate.install_gate("guide")


def test_the_enterprise_tool_table_is_not_imported_from_a_planted_package_either(tmp_path, monkeypatch):
    plant(tmp_path, monkeypatch, "context_runtime_enterprise")
    assert "guide.planted" not in tool_actions.resolve_tool_actions("guide")
    assert tool_actions._enterprise_table("guide") is None and nothing_ran(tmp_path)


def test_the_engine_default_lookup_does_not_import_a_planted_engine(tmp_path, monkeypatch):
    plant(tmp_path, monkeypatch, "context_runtime")
    assert app_gate._cr_default_authorizer() is None and nothing_ran(tmp_path)
    assert app_gate._engine_importable() is False and nothing_ran(tmp_path)
