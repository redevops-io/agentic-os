"""The frozen sidecar must not depend on where the build interpreter is installed (D-NEW-4).

The 0.6.0-rc1 Linux qualification built the same tree twice from different places (a user build and a root snapcraft build) and the
sidecar bytes differed, which changed the pass-1 SBOM digest and the Licenses files. The PYZ module that differs is the interpreter's
`_sysconfigdata__<platform>`: uv rewrites the install prefix into it, so it holds the path of the interpreter (~25 strings), and the
frozen app carries that build-host path. (The second module the qualification named, `agentic_os.intelligence.adapters`, is a namespace
package whose PYZ entry extracts to None; the comparison tool counted every None as different. Not a real difference.)

packaging/tools/normalise_sysconfigdata.py replaces the interpreter prefix with a fixed token in that one module's source before it is
compiled into the archive. Offline: synthetic text, the running interpreter's own file, and the spec with a stubbed PyInstaller.
"""
from __future__ import annotations

import importlib.util
import sys
import sysconfig
from pathlib import Path

import pytest

from tests.test_sidecar_excludes import ROOT, _run_spec

TOOL = ROOT / "packaging" / "tools" / "normalise_sysconfigdata.py"


def _tool():
    spec = importlib.util.spec_from_file_location("normalise_sysconfigdata", TOOL)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


SAMPLE = '''build_time_vars = {
    "BINDIR": "/home/u1/.local/share/uv/python/cpython-3.12.10-linux-aarch64-gnu/bin",
    "LIBDIR": "/home/u1/.local/share/uv/python/cpython-3.12.10-linux-aarch64-gnu/lib",
    "CONFIG_ARGS": "'--prefix=/install' '--with-openssl=/tmp/tmpabc/tools/deps'",
    "DESTDIRS": "/home/u1/.local/share/uv/python/cpython-3.12.10-linux-aarch64-gnu /home/u1/.local/share/uv/python/cpython-3.12.10-linux-aarch64-gnu/lib",
    "VERSION": "3.12",
}
'''


def test_the_prefix_is_replaced_everywhere_and_nothing_else_changes():
    t = _tool()
    out = t.normalise(SAMPLE, ["/home/u1/.local/share/uv/python/cpython-3.12.10-linux-aarch64-gnu"])
    assert "/home/u1" not in out and out.count(t.TOKEN) == 4
    assert "'--prefix=/install'" in out and "/tmp/tmpabc/tools/deps" in out and '"VERSION": "3.12"' in out     # build-time constants stay
    compile(out, "x", "exec")


def test_two_install_locations_give_identical_normalised_text():
    t = _tool()
    a = SAMPLE
    b_prefix = "/root/.local/share/uv/python/cpython-3.12.10-linux-aarch64-gnu"
    a_prefix = "/home/u1/.local/share/uv/python/cpython-3.12.10-linux-aarch64-gnu"
    b = a.replace(a_prefix, b_prefix)
    assert a != b
    assert t.normalise(a, [a_prefix]) == t.normalise(b, [b_prefix])


def test_longest_prefix_wins_and_empty_or_root_prefixes_are_ignored():
    t = _tool()
    out = t.normalise('X = "/opt/py/lib /opt/py"\n', ["/opt", "/opt/py", "", "/"])
    assert out == 'X = "' + t.TOKEN + '/lib ' + t.TOKEN + '"\n'


def test_a_prefix_inside_a_longer_name_is_not_cut():
    t = _tool()
    out = t.normalise('X = "/opt/python-extra/lib"\n', ["/opt/python"])
    assert out == 'X = "/opt/python-extra/lib"\n'


def test_the_running_interpreters_sysconfigdata_has_no_install_path_after_normalisation():
    t = _tool()
    mods = [m for m in sys.modules if m.startswith("_sysconfigdata")] or []
    name = sysconfig._get_sysconfigdata_name() if hasattr(sysconfig, "_get_sysconfigdata_name") else None
    if not name or sys.platform == "win32":
        pytest.skip("no _sysconfigdata module on this platform")
    import importlib
    mod = importlib.import_module(name)
    src = Path(mod.__file__).read_text(encoding="utf-8")
    prefixes = {sys.base_prefix, sys.prefix, sys.exec_prefix, sys.base_exec_prefix}
    out = t.normalise(src, prefixes)
    for p in prefixes:
        assert p not in out
    compile(out, name, "exec")
    ns: dict = {}
    exec(compile(out, name, "exec"), ns)
    assert ns["build_time_vars"].keys() == mod.build_time_vars.keys()


def test_normalise_pure_rewrites_only_sysconfigdata_modules_and_writes_to_the_work_dir(tmp_path):
    t = _tool()
    sc = tmp_path / "src" / "_sysconfigdata__linux_x86_64-linux-gnu.py"
    other = tmp_path / "src" / "other.py"
    sc.parent.mkdir()
    sc.write_text(SAMPLE)
    other.write_text('P = "/home/u1/.local/share/uv/python/cpython-3.12.10-linux-aarch64-gnu"\n')
    pure = [("_sysconfigdata__linux_x86_64-linux-gnu", str(sc), "PYMODULE"), ("other", str(other), "PYMODULE"),
            ("pkg", str(tmp_path / "pkg" / "__init__.py"), "PYMODULE")]
    work = tmp_path / "work"
    out = t.normalise_pure(pure, work, ["/home/u1/.local/share/uv/python/cpython-3.12.10-linux-aarch64-gnu"])
    assert out[1] == pure[1] and out[2] == pure[2]                                         # untouched
    assert out[0][0] == pure[0][0] and out[0][2] == "PYMODULE" and Path(out[0][1]).parent == work
    assert "/home/u1" not in Path(out[0][1]).read_text()
    assert "/home/u1" in sc.read_text()                                                     # the interpreter's own file is not modified


def test_the_sidecar_spec_runs_the_normalisation_on_the_analysis_result():
    src = (ROOT / "packaging" / "redevops-sidecar.spec").read_text()
    assert "normalise_sysconfigdata" in src and "normalise_pure" in src
    assert src.index("normalise_pure") > src.index("a = Analysis(") and src.index("normalise_pure") < src.index("pyz = PYZ(")


def test_the_spec_still_loads_with_the_hook_and_an_empty_pure_list():
    a = _run_spec({"SPECPATH": str(ROOT / "packaging")})
    assert "site" in a["excludes"]
