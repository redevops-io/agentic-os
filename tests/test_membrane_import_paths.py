"""What the contained child can import. The sandbox hands the child the parent's import paths so the capability resolves, but they must never be able to shadow the standard library: a directory on the parent's
path that holds an `operator.py` or a `json.py` (an app directory does) used to break the child before it ran anything, because PYTHONPATH sits ahead of the standard library. Platform-independent: no memory
ceiling is asked for, so these run on macOS too."""
from __future__ import annotations

import os
import sysconfig

import pytest

from runtime_contracts.models import ExecutionConstraint
from agentic_os.mission.membrane import LocalContainmentSandbox

CAP = "agentic_os.mission._membrane_selftest"


def sandbox(**kw):
    return LocalContainmentSandbox(constraint=ExecutionConstraint(max_memory_mb=0, max_duration_seconds=20, max_processes=0), **kw)


def write(tmp_path, name, body):
    d = tmp_path / "proj"
    d.mkdir(exist_ok=True)
    (d / name).write_text(body)
    return d


@pytest.mark.parametrize("shadow", ["operator.py", "json.py", "re.py", "os.py"])
def test_a_directory_that_shadows_a_standard_library_module_cannot_break_the_child(tmp_path, monkeypatch, shadow):
    d = write(tmp_path, shadow, "raise RuntimeError('this file must never be imported in place of the standard library')\n")
    monkeypatch.syspath_prepend(str(d))                                       # on the parent's path, first, as an app directory is
    assert sandbox().invoke("op", f"{CAP}:echo", {"x": 1}, "k") == {"echo": {"x": 1}}


def test_a_capability_that_lives_on_the_parents_path_is_still_importable_in_the_child(tmp_path, monkeypatch):
    d = write(tmp_path, "membrane_probe_cap.py", "def hello(inputs):\n    return {'hi': inputs.get('n')}\n")
    monkeypatch.syspath_prepend(str(d))
    assert sandbox().invoke("op", "membrane_probe_cap:hello", {"n": 7}, "k") == {"hi": 7}


def test_the_childs_path_order_is_standard_library_then_the_parents_paths_then_site_packages(tmp_path, monkeypatch):
    d = write(tmp_path, "membrane_probe_path.py", "def where(inputs):\n    import sys\n    return {'path': list(sys.path)}\n")
    monkeypatch.syspath_prepend(str(d))
    path = sandbox().invoke("op", "membrane_probe_path:where", {}, "k")["path"]
    stdlib = sysconfig.get_paths()["stdlib"]
    mine = path.index(str(d))
    assert path.index(stdlib) < mine                                            # the standard library always wins
    sp = [i for i, p in enumerate(path) if p.endswith(("site-packages", "dist-packages"))]
    if sp:
        assert mine < sp[0]                                                      # project code still beats an installed copy


def test_the_childs_environment_is_still_scrubbed_and_carries_no_pythonpath(tmp_path, monkeypatch):
    d = write(tmp_path, "membrane_probe_env.py", "def env(inputs):\n    import os\n    return {'keys': sorted(os.environ)}\n")
    monkeypatch.syspath_prepend(str(d))
    monkeypatch.setenv("MEMBRANE_TEST_SECRET", "super-secret-value")
    keys = sandbox().invoke("op", "membrane_probe_env:env", {}, "k")["keys"]
    assert "PYTHONPATH" not in keys and "MEMBRANE_TEST_SECRET" not in keys and "MEMBRANE_PATH" in keys


def test_an_entry_already_on_the_childs_path_is_not_added_twice(tmp_path, monkeypatch):
    d = write(tmp_path, "membrane_probe_dup.py", "def where(inputs):\n    import sys\n    return {'path': list(sys.path)}\n")
    monkeypatch.syspath_prepend(str(d))
    monkeypatch.syspath_prepend(str(d))
    path = sandbox().invoke("op", "membrane_probe_dup:where", {}, "k")["path"]
    assert path.count(str(d)) == 1
