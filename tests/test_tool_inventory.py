"""The classification inventory (docs/TOOL_CLASSIFICATION_INVENTORY.md): one row per tool, kept equal to the registry. Classification is not enforcement, and the sink heuristic cannot prove a
tool read-only; the inventory is the written record of how each tool was classified, and this test is what keeps it honest."""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

from agentic_os.tool_actions import TOOL_ACTIONS

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "TOOL_CLASSIFICATION_INVENTORY.md"
CATEGORIES = {"read-only-state", "writes-local-or-persistent-artifacts", "launches external process or network", "dynamically selected behaviour", "public intake: unauthenticated by design"}


def rows():
    out = []
    for line in DOC.read_text().splitlines():
        m = re.match(r"^\| `([^`]+)` \| ([^|]+) \| (read|write|delete) \| ([^|]+) \| ([^|]+) \| ([^|]+) \| ([^|]+) \|$", line)
        if m:
            out.append(tuple(x.strip() for x in m.groups()))
    return out


def test_every_classified_tool_has_exactly_one_row_and_no_row_is_for_an_unknown_tool():
    got = [(app, tool, action) for tool, app, action, *_ in rows()]
    want = [(app, tool, action) for app, tools in TOOL_ACTIONS.items() for tool, action in tools.items()]
    assert len(got) == len(set(got)) and sorted(got) == sorted(want)


def test_the_inventory_is_what_the_generator_produces_from_the_registry():
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "generate_tool_inventory.py")], capture_output=True, text=True, timeout=120, cwd=str(ROOT))
    assert out.returncode == 0 and out.stdout == DOC.read_text(), "regenerate: python3 scripts/generate_tool_inventory.py > docs/TOOL_CLASSIFICATION_INVENTORY.md"


def test_every_row_has_a_category_real_evidence_a_reviewer_and_an_approval_field():
    for tool, app, action, cat, ev, approval, reviewed in rows():
        assert cat in CATEGORIES, (tool, cat)
        path, _, line = ev.rpartition(":")
        f = ROOT / path
        assert f.is_file() and int(line) >= 1, (tool, ev)
        assert re.search(rf"""["']{re.escape(tool)}["']""", f.read_text(errors="replace").splitlines()[int(line) - 1]), (tool, ev)
        assert approval.split(" ")[0] in ("yes", "no") and "reviewed" not in reviewed.lower() or "2026" in reviewed, (tool,)
        assert reviewed and "2026" in reviewed


def test_a_tool_whose_behaviour_is_chosen_by_the_caller_is_never_classified_read():
    """The strictest-action rule: dynamically selected behaviour (plan/apply, scan with options, free-form targets) is classified at the strictest action it can perform."""
    for tool, app, action, cat, *_ in rows():
        if cat == "dynamically selected behaviour":
            assert action != "read", (app, tool)


def test_a_tool_that_launches_a_process_or_calls_out_is_never_classified_read_except_the_reviewed_reads():
    reviewed_reads = {"sentinel.triage", "outreach.refresh", "bi.ask", "bi.refresh", "bi.summary", "growth.analyze", "infra.verify", "document.connect_check", "document.inspect",
                      "document.open_session", "document.propose_patch"}
    for tool, app, action, cat, *_ in rows():
        if cat == "launches external process or network":
            assert action != "read" or tool in reviewed_reads, (app, tool)


def test_the_inventory_states_the_limits_of_the_heuristic():
    t = DOC.read_text()
    assert "Classification is not enforcement" in t and "cannot prove" in t and "strictest action" in t and "separate checks" in t


def test_the_public_intake_row_type_is_exactly_the_registered_public_routes_and_the_action_stays_write():
    from agentic_os.tool_actions import PUBLIC_INTAKE_ROUTES
    want = {(app, tool) for app, routes in PUBLIC_INTAKE_ROUTES.items() for tool in routes.values()}
    got = {(app, tool) for tool, app, action, cat, *_ in rows() if cat == "public intake: unauthenticated by design"}
    assert got == want and want
    assert all(action == "write" for tool, app, action, cat, *_ in rows() if cat == "public intake: unauthenticated by design")
