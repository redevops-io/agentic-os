"""A test must not close or replace a file descriptor it does not own (tests/conftest.py: ``_fd_integrity_guard``).

pytest's faulthandler plugin keeps a duplicate of stderr in a descriptor it owns and closes it in ``pytest_unconfigure``, after the last test. A test that
``dup2``-ed over that number (and closed it) left the run with ``OSError: [Errno 9] Bad file descriptor`` at the very end and exit status 1 although every
test had passed (tests/test_signing_procedure_r0prime.py::test_r5_inherited_file_descriptors_are_closed did exactly that). The guard makes such a test fail
at its own teardown instead; it neither restores the descriptor nor changes the exit status.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tests._proc_support import child_env

ROOT = Path(__file__).resolve().parent.parent
CULPRIT = "tests/test_signing_procedure_r0prime.py::test_r5_inherited_file_descriptors_are_closed"

# The sample tests replace the descriptor pytest saved for faulthandler (the way the old signing test did), or only use their own descriptors.
SAMPLE = '''
import os
from _pytest.faulthandler import fault_handler_stderr_fd_key


def test_clean_uses_only_its_own_descriptors():
    r, w = os.pipe()
    os.close(r)
    os.close(w)


def test_clobbers_the_descriptor_pytest_saved(request):
    fd = request.config.stash[fault_handler_stderr_fd_key]
    other = os.open(os.devnull, os.O_RDONLY)
    os.dup2(other, fd)          # replaces what faulthandler's stderr copy points at ...
    os.close(other)
    os.close(fd)                # ... and closes it: pytest_unconfigure will find it gone


def test_clean_after_the_clobbering_test():
    pass
'''


def _session(tmp: Path, sample: str, *args: str):
    (tmp / "conftest.py").write_text((ROOT / "tests" / "conftest.py").read_text())
    (tmp / "test_sample.py").write_text(sample)
    env = {k: v for k, v in child_env().items() if not k.startswith(("REDEVOPS_ALLOW", "PYTEST"))}
    return subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", *args, str(tmp)], capture_output=True, text=True, env=env, cwd=tmp, timeout=300)


def test_a_test_that_closes_the_saved_descriptor_fails_at_its_own_teardown(tmp_path):
    r = _session(tmp_path, SAMPLE)
    out = r.stdout + r.stderr
    assert r.returncode != 0, out
    # reported at the offending test, by name, and only there
    assert "ERROR at teardown of test_clobbers_the_descriptor_pytest_saved" in out, out
    assert "test_clean_uses_only_its_own_descriptors" not in out.split("short test summary info")[-1], out
    assert "test_clean_after_the_clobbering_test" not in out.split("short test summary info")[-1], out
    assert "3 passed, 1 error" in out, out
    # the guard does not hide anything: pytest's own unconfigure still reports the lost descriptor
    assert "Bad file descriptor" in out, out


def test_a_session_whose_tests_keep_their_hands_off_the_saved_descriptor_is_clean(tmp_path):
    clean = SAMPLE.split("def test_clobbers", 1)[0]
    r = _session(tmp_path, clean)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "1 passed" in out and "Bad file descriptor" not in out, out


def test_the_signing_test_that_used_to_close_it_leaves_pytests_descriptors_alone():
    """The test that used to dup2 over descriptors 9, 12, 20, 64, 100 and 200 of the pytest process: run alone in its own session, the session must end with
    status 0 and no OSError from faulthandler's unconfigure."""
    r = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", CULPRIT], capture_output=True, text=True, env=child_env(), cwd=ROOT, timeout=300)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "1 passed" in out and "Bad file descriptor" not in out and "ERROR at teardown" not in out, out


def test_the_descriptor_the_guard_watches_is_open_and_is_a_copy_of_standard_error(request):
    """Control for the samples above: the number they target is open during a test and is not 0, 1 or 2."""
    from _pytest.faulthandler import fault_handler_stderr_fd_key
    fd = request.config.stash[fault_handler_stderr_fd_key]
    assert fd > 2
    os.fstat(fd)
