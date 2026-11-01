"""Marks for tests that assert a POSIX-only mechanism. A test that proves a property by chmod, uid or directory fsync cannot express it
on Windows: it is skipped there with the reason, and the same property is proved from the DACL by the Windows tests in
tests/test_platform_files.py (TestWindowsPrivateFiles) and tests/test_private_files_windows.py. Never a skip for behaviour Windows
must provide without a test of that behaviour."""
import os

import pytest

WINDOWS = os.name == "nt"

posix_modes = pytest.mark.skipif(
    WINDOWS, reason="asserts POSIX mode bits or uids (chmod/geteuid); on Windows the same refusal is decided from the DACL and tested in "
                    "tests/test_platform_files.py and tests/test_private_files_windows.py")

needs_dir_fsync = pytest.mark.skipif(
    WINDOWS, reason="the injected fsync numbering counts directory fsyncs, which Windows cannot perform: there the third and fourth "
                    "fsync are the counter-file write, outside the migration's guarded region")

posix_shell_recipes = pytest.mark.skipif(
    WINDOWS, reason="runs the Makefile recipes and a /bin/sh shim; GNU make and a POSIX shell are not part of the native core")
