"""Regression: `redevops-sidecar bootstrap` must not die with UnicodeEncodeError (R0-prime defect a).

The launcher runs the sidecar with stdout redirected to a pipe. On Windows that means the ANSI code
page (cp1252) and on POSIX the locale codec; neither can encode U+2192 (and latin-1 cannot encode the
U+2014 em dash on the first line either), so on v0.3.2 `bootstrap` crashed with a traceback and exit 1
instead of printing the report. The frozen PyInstaller binary ignores PYTHONIOENCODING / PYTHONUTF8
(isolated mode), so the fix lives in `packaging/sidecar_main.py` (UTF-8 reconfigure at startup).

These tests run the real entry script as a subprocess under CPython with redirected stdout and a
simulated legacy encoding, and compare with a UTF-8 baseline. The frozen-binary equivalent (locale
matrix on the built executable) is `packaging/check-sidecar-locales.sh`.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ENTRY = REPO / "packaging" / "sidecar_main.py"

# legacy encodings that exist on real machines: Western-European Windows, Latin-1 POSIX locales,
# Cyrillic Windows, and strict ASCII (LANG=C on old systems).
LEGACY = ["cp1252", "latin-1", "cp1251", "ascii", "cp437"]


def _run(tmp_path: Path, *, ioencoding: str | None, extra_env: dict | None = None, args=("bootstrap",)):
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),
        "PYTHONPATH": str(REPO),
        "RDO_DISABLE_PAIR": "1",  # keep the report independent of a local PAIR node
    }
    if "SYSTEMROOT" in os.environ:  # Windows needs it for the interpreter to start
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    if ioencoding:
        env["PYTHONIOENCODING"] = ioencoding
    env.update(extra_env or {})
    # stdout/stderr are files, exactly like a redirected / piped run (not a console).
    out, err = tmp_path / "stdout.bin", tmp_path / "stderr.bin"
    with open(out, "wb") as so, open(err, "wb") as se:
        proc = subprocess.run([sys.executable, str(ENTRY), *args], env=env, cwd=tmp_path,
                              stdin=subprocess.DEVNULL, stdout=so, stderr=se, timeout=120)
    return proc.returncode, out.read_bytes(), err.read_bytes().decode("utf-8", "replace")


def _baseline(tmp_path: Path):
    """The report with a UTF-8 stdout (what the launcher decodes): the reference for every locale."""
    d = tmp_path / "baseline"
    d.mkdir()
    return _run(d, ioencoding="utf-8")


def test_baseline_report_is_utf8_with_the_non_cp1252_characters(tmp_path):
    rc, out, err = _run(tmp_path, ioencoding="utf-8")
    assert rc in (0, 1), err[-500:]
    text = out.decode("utf-8")
    assert "ReDevOps — device readiness" in text
    # the arrow line exists only when no LLM is configured, which is the (cleared) test environment
    assert "→ get a free key" in text


@pytest.mark.parametrize("enc", LEGACY)
def test_bootstrap_output_is_identical_under_a_legacy_stdout_encoding(tmp_path, enc):
    base_rc, base_out, _ = _baseline(tmp_path)
    sub = tmp_path / enc
    sub.mkdir()
    rc, out, err = _run(sub, ioencoding=enc)
    assert "UnicodeEncodeError" not in err, err[-800:]
    assert rc == base_rc, f"exit code must stay the true verdict ({base_rc}), got {rc}\n{err[-500:]}"
    assert out == base_out, "stdout must be byte-identical to the UTF-8 run"


@pytest.mark.parametrize("enc", ["cp1252", "latin-1"])
def test_bootstrap_survives_pythonutf8_off_and_a_non_utf8_environment(tmp_path, enc):
    base_rc, base_out, _ = _baseline(tmp_path)
    sub = tmp_path / "x"
    sub.mkdir()
    rc, out, err = _run(sub, ioencoding=enc, extra_env={"PYTHONUTF8": "0"})
    assert "UnicodeEncodeError" not in err, err[-800:]
    assert (rc, out) == (base_rc, base_out)


def test_stderr_is_utf8_too(tmp_path):
    """A traceback/log line with a non-ASCII character must not raise on a legacy stderr."""
    code = (
        "import runpy, sys; sys.argv=['sidecar_main.py']; "
        "m = runpy.run_path(%r); m['_utf8_stdio'](); "
        "print('\\u2192 out'); print('\\u2192 err', file=sys.stderr)" % str(ENTRY)
    )
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONIOENCODING": "cp1252", "PYTHONPATH": str(REPO)}
    if "SYSTEMROOT" in os.environ:
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    with open(tmp_path / "o", "wb") as so, open(tmp_path / "e", "wb") as se:
        p = subprocess.run([sys.executable, "-c", code], env=env, stdout=so, stderr=se, timeout=60)
    assert p.returncode == 0, (tmp_path / "e").read_bytes().decode("utf-8", "replace")
    assert (tmp_path / "o").read_bytes().decode("utf-8").strip() == "→ out"
    assert (tmp_path / "e").read_bytes().decode("utf-8").strip() == "→ err"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX locale simulation")
@pytest.mark.parametrize("locale_name", ["en_US.ISO8859-1", "en_US.US-ASCII", "ru_RU.CP1251"])
def test_bootstrap_under_a_real_non_utf8_locale_when_the_host_has_it(tmp_path, locale_name):
    import locale
    saved = locale.setlocale(locale.LC_CTYPE)
    try:
        try:
            locale.setlocale(locale.LC_CTYPE, locale_name)
        except locale.Error:
            pytest.skip(f"locale {locale_name} is not installed on this host")
    finally:
        locale.setlocale(locale.LC_CTYPE, saved)
    base_rc, base_out, _ = _baseline(tmp_path)
    sub = tmp_path / "loc"
    sub.mkdir()
    # no PYTHONIOENCODING: the codec comes from the locale, as on a user machine
    rc, out, err = _run(sub, ioencoding=None,
                        extra_env={"LC_ALL": locale_name, "LANG": locale_name, "PYTHONUTF8": "0",
                                   "PYTHONCOERCECLOCALE": "0"})
    assert "UnicodeEncodeError" not in err, err[-800:]
    assert (rc, out) == (base_rc, base_out)
