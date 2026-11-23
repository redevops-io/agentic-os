"""One table decides what a tool costs. A second table is allowed only on a different axis, and guarded.

``agentic_os.tool_actions.TOOL_ACTIONS`` is the classification ``make_authorizer`` reads: the grant a call is
checked for. Its authority is only worth anything while it is the only place that answer comes from, so this
file checks the two ways it could stop being one.

The first is a copy drifting away from it. An AST sweep of the package finds every module-level table that
grades tools, and this repository has exactly one besides the kernel's: ``products.schemas.TOOL_CLASS``. That
one is legitimate — it states what the *product service* demands of a caller ("a stage creates a proposal only;
a person decides", "bounded write: service token + product role + product approval policy") — but it is a
different axis in a different vocabulary, and the day a capability appears in both tables is the day the two
can disagree about how much authority a call needs.

The second is the wrong table being plugged into the plane. Both are checked the way ``resolve_tool_actions``
already settles an enterprise cross-check: not by hoping, but by refusing a disagreement.
"""
from __future__ import annotations

import ast
import pathlib

from agentic_os.permissions import ACTIONS
from agentic_os.permissions_setup import bundled_tools
from agentic_os.products import schemas
from agentic_os.tool_actions import TOOL_ACTIONS, resolve_tool_actions

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "agentic_os"

#: The kernel's own table, found by name wherever it is written.
_KERNEL = "TOOL_ACTIONS"
#: A graded table that is allowed to exist because it grades something else: what the product service
#: requires of the caller, not which grant the platform matches the call against.
_ALLOWED_OTHER = {"agentic_os/products/schemas.py::TOOL_CLASS"}

#: The least grant class a product-side class is compatible with. ``stage`` writes a proposal somebody has
#: to approve, and ``bounded_write`` writes under a product role: both are writes in the platform's
#: vocabulary, whatever the product calls them.
_PRODUCT_FLOOR = {"read": "read", "present": "read", "stage": "write", "bounded_write": "write"}

_RANK = {action: rank for rank, action in enumerate(ACTIONS)}


def _graded_tables() -> dict[str, dict[str, str]]:
    """``{"<path>::<TABLE>": {tool: class}}`` for every module-level dict under agentic_os/ that grades tools.

    "Grades" means the values are risk classes from either vocabulary and the keys are dotted capability
    names — the shape of a classification table, found by structure rather than by name, so a renamed copy
    is still found.
    """
    known = set(ACTIONS) | set(_PRODUCT_FLOOR)
    found: dict[str, dict[str, str]] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        for node in tree.body:
            names = ([t.id for t in node.targets if isinstance(t, ast.Name)] if isinstance(node, ast.Assign)
                     else [node.target.id] if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
                     else [])
            if not isinstance(getattr(node, "value", None), ast.Dict):
                continue
            rows: dict[str, str] = {}
            for key, value in zip(node.value.keys, node.value.values):
                if (isinstance(key, ast.Constant) and isinstance(key.value, str) and "." in key.value
                        and isinstance(value, ast.Constant) and value.value in known):
                    rows[key.value] = value.value
            if len(rows) >= 3:
                for name in names:
                    found[f"{path.relative_to(ROOT)}::{name}"] = rows
    return found


def test_the_kernel_table_grades_every_tool_it_claims_in_the_shared_vocabulary():
    flat = {tool: action for table in TOOL_ACTIONS.values() for tool, action in table.items()}
    assert len(flat) > 100, "the kernel table moved or emptied: this guard would check nothing"
    assert set(flat.values()) <= set(ACTIONS), "a tool is classified outside the grant vocabulary"


def test_the_platform_holds_exactly_one_grant_table_besides_the_product_contract():
    """No second copy of the answer, under any name."""
    others = {key for key, rows in _graded_tables().items() if key.split("::")[-1] != _KERNEL}
    assert others <= _ALLOWED_OTHER, (
        f"{sorted(others - _ALLOWED_OTHER)} grade tools too: a second classification table drifts from "
        f"TOOL_ACTIONS, and the plane can only ever read one of them")


def test_the_product_table_is_a_different_axis_not_a_second_grant_table():
    """It grades what the product service demands, which is why it uses classes the plane has never heard of."""
    assert set(schemas.TOOL_CLASS.values()) - set(ACTIONS), (
        "TOOL_CLASS now uses only grant vocabulary: it has become a second grant table — fold it into "
        "TOOL_ACTIONS or say which one the plane reads")


def test_a_tool_the_product_gates_is_never_looser_in_the_kernel_than_the_product_admits():
    """Two tables may hold the same capability only while they agree about its floor."""
    # Joined on the capability, not on the string: the product writes "quality.qa_start_run" and an app
    # registers "qa_start_run", and a join on the whole name would call those two different tools.
    by_capability: dict[str, list[tuple[str, str]]] = {}
    for app, table in TOOL_ACTIONS.items():
        for tool, action in table.items():
            by_capability.setdefault(tool.split(".")[-1], []).append((f"{app}.{tool}", action))
    clashing = []
    for tool, product_class in schemas.TOOL_CLASS.items():
        floor = _PRODUCT_FLOOR[product_class]
        for kernel_tool, kernel_class in by_capability.get(tool.split(".")[-1], []):
            if _RANK[kernel_class] < _RANK[floor]:
                clashing.append((tool, product_class, floor, kernel_tool, kernel_class))
    assert not clashing, (
        f"{clashing}: the product says a caller needs {clashing[0][2]!r} at least, and the kernel table it "
        f"reads would let a weaker grant through — either the kernel class rises or the product's claim does")


def test_the_two_tables_are_disjoint_until_someone_deliberately_overlaps_them():
    """What the guard above is currently resting on, stated where it can be watched.

    The product's capabilities and the kernel's do not overlap by name today, which is the only reason the
    clash check passes without exercising its comparison. This asserts the overlap set stays empty so that
    the first person to introduce one lands on a failing test that explains the choice, not on a green one.
    """
    kernel_short = {tool.split(".")[-1] for table in TOOL_ACTIONS.values() for tool in table}
    overlapping = sorted(t for t in schemas.TOOL_CLASS if t.split(".")[-1] in kernel_short)
    assert overlapping == [], (
        f"{overlapping} now appear in both tables: the clash check above is live for them — read what each "
        f"call does and make the kernel table at least as strict as what the product demands")


def test_the_plane_reads_the_kernel_table_and_nothing_else():
    """The bootstrap's inputs are the kernel table itself, not a restatement of it."""
    flat = {tool: action for table in TOOL_ACTIONS.values() for tool, action in table.items()}
    assert bundled_tools() == {tool: (app, action) for app, table in TOOL_ACTIONS.items()
                              for tool, action in table.items()}
    for app in TOOL_ACTIONS:
        assert resolve_tool_actions(app, enterprise=False) == TOOL_ACTIONS[app]