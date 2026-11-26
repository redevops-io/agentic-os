"""The WebAssembly probe is run by hand at release time, so its expectations drift unnoticed. They are held here to what
the kernel reports."""
import pathlib
import re

from agentic_os.dashboards.portable import PortableDashboards

PROBE = (pathlib.Path(__file__).resolve().parents[1] / "packaging" / "wasm" / "dashboards_probe.mjs").read_text()


def test_the_probe_expects_as_many_dashboards_to_need_an_application_as_the_kernel_says():
    about = PortableDashboards().handle("GET", "/api/dashboards/_portable")[1]
    expected = int(re.search(r"const NEEDS_AN_APPLICATION = (\d+);", PROBE).group(1))
    assert expected == len(about["unavailable"]), sorted(d["id"] for d in about["unavailable"])
