"""The dashboards kernel archive really runs on its own: built from this checkout, then every dashboard is exercised by an
interpreter whose ``agentic_os`` is ONLY the archive (no checkout, no installed kernel). A module or a package marker
missing from the archive fails here; v0.4.24 shipped an archive without one package, and nothing had run it."""
import importlib.util
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_kernel_zip", ROOT / "packaging" / "wasm" / "build_kernel_zip.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def test_every_dashboard_runs_from_the_archive_alone(tmp_path):
    out = tmp_path / "kernel.zip"
    built = subprocess.run([sys.executable, str(ROOT / "packaging" / "wasm" / "build_kernel_zip.py"), str(out)], capture_output=True,
                           text=True, timeout=300, cwd=str(ROOT))
    assert built.returncode == 0, built.stderr[-3000:]
    # The archive goes first on the path, so ``agentic_os`` is the archive's package and nothing missing from it can be
    # found anywhere else (a submodule is only looked up inside its package). PyYAML comes from the interpreter, as the
    # browser page loads it beside the archive.
    alone = "import sys; sys.path.insert(0, sys.argv[1])\n" + builder.EXERCISE + (
        "\nforeign = [(n, f) for n, f in mods if not f.startswith(sys.argv[1])]\nassert mods and not foreign, foreign[:5]\n")
    ran = subprocess.run([sys.executable, "-I", "-c", alone, str(out)], capture_output=True, text=True, timeout=300, cwd=str(tmp_path))
    assert ran.returncode == 0, ran.stderr[-3000:]
