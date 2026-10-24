"""Platform facts the suite needs to be honest about. A test that proves a property the platform cannot provide is SKIPPED there with the reason, never made to pass: CI runs on Linux, where it runs."""
import pytest

from agentic_os.mission.membrane import memory_ceiling_enforceable

needs_memory_ceiling = pytest.mark.skipif(
    not memory_ceiling_enforceable(),
    reason="this platform cannot apply an address-space ceiling (RLIMIT_AS; macOS refuses it), so the membrane refuses to run with one and the property cannot be tested here; Linux CI runs it")
