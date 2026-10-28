#!/usr/bin/env python3
# Copyright (c) 2025-2026 RedevOps.
# Licensed under the GNU Affero General Public License v3.0 or later, as modified by the Commons Clause License (see LICENSE.md).
"""Generate docs/TOOL_CLASSIFICATION_INVENTORY.md: one row per classified tool (the inventory behind agentic_os/tool_actions.py).

Columns: tool, app, action, category, evidence (file:line where the tool is declared), approval needed (the operator capability is approval_required), reviewed-by / date.
The category is a reviewed judgement recorded in CATEGORY_OVERRIDES / the sets below (not inferred from names); everything not listed there falls back to the action:
read -> ``read-only-state``, write/delete -> ``writes-local-or-persistent-artifacts``. A test (tests/test_tool_inventory.py) fails when a classified tool has no row or the rows
disagree with the registry. The sink heuristic (tests/test_read_classification_sinks.py) is a regression signal only: it cannot prove a tool read-only (indirect execution, imported helpers,
subprocesses behind an injected runner all escape a static scan), so the strictest-action rule applies to anything with caller-selected behaviour.

    python3 scripts/generate_tool_inventory.py > docs/TOOL_CLASSIFICATION_INVENTORY.md
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentic_os.tool_actions import PUBLIC_INTAKE_ROUTES, TOOL_ACTIONS  # noqa: E402

PUBLIC_INTAKE_TOOLS = {(app, tool) for app, routes in PUBLIC_INTAKE_ROUTES.items() for tool in routes.values()}

REVIEWER = "reviewed in the 2026-10 AI-assisted permissions security review (not an external audit)"
CATEGORIES = ("read-only-state", "writes-local-or-persistent-artifacts", "launches external process or network", "dynamically selected behaviour", "public intake: unauthenticated by design")

# launches an external process, or reads/writes another system over the network (a scan, a deploy, a CRM/e-mail/payment API, an editor service)
EXTERNAL = {
    "supply_chain_scan", "list_vulnerabilities", "scan_app", "scan_apps", "sbom", "malware_scan", "scan_deployment", "ban_ip", "sentinel.block_ip", "sentinel.unblock_ip",
    "propose_network_policy", "compliance.scan", "compliance.remediate", "compliance.file_consent", "billing.dunning", "billing.refund", "billing.create_subscription",
    "billing.cancel_subscription", "chase_overdue", "refund", "books.categorize", "books.close", "books.record_revenue", "books.reverse_entry", "infra.provision",
    "infra.destroy_delta", "outreach.approve", "outreach.send_all", "outreach.discover", "social.publish", "social.record_reel", "social.vibex_generate", "assistant.push_assets",
    "crm.score_lead", "crm.research", "crm.draft_outreach", "crm.qualify", "lifecycle.compose_campaign", "support.draft_reply", "support.resolve", "support.escalate",
    "support.send_onboarding", "document.publish", "document.apply_patch", "document.set_label", "growth.reallocate_budget", "radar.add_watch", "add_watch", "privacy.intake",
    "privacy.access", "privacy.delete", "social.draft", "social.draft_visual",
}
# the behaviour is chosen by the caller's input (a mode flag, a playbook/target/vars argument, a free-form query): classified at the strictest action it can perform
DYNAMIC = {"infra.plan", "infra.drift", "infra.configure", "infra.rollback_release", "infra.provision", "infra.destroy_delta", "compliance.scan", "scan_app", "scan_apps",
           "scan_deployment", "malware_scan", "sbom", "supply_chain_scan", "list_vulnerabilities", "document.convert", "document.export", "outreach.discover"}
LOCAL = {"assistant.playbook", "assistant.subreddit_plan", "assistant.founder_content", "assistant.community_blueprint", "assistant.cold_outreach", "assistant.hire_brief",
         "document.convert", "document.export"}


def category(tool: str, action: str, app: str = "") -> str:
    if (app, tool) in PUBLIC_INTAKE_TOOLS:                    # the tool stays `write` for every ordinary interface; a registered public route may ALSO open a pending request of it without a grant
        return CATEGORIES[4]
    if tool in DYNAMIC:
        return CATEGORIES[3]
    if tool in LOCAL:
        return CATEGORIES[1]
    if tool in EXTERNAL:
        return CATEGORIES[2]
    return CATEGORIES[0] if action == "read" else CATEGORIES[1]


def evidence(app: str, tool: str) -> str:
    base = ROOT / "apps" / app
    for name in ("operator.py", "app.py", "assistant.py", "core.py") + tuple(sorted(p.name for p in base.glob("*.py") if p.name not in ("operator.py", "app.py", "assistant.py", "core.py"))):
        f = base / name
        if not f.is_file() or name.startswith("test_"):
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if re.search(rf"""["']{re.escape(tool)}["']""", line):
                return f"apps/{app}/{name}:{i}"
    return "UNFOUND"


def approval(app: str, tool: str) -> str:
    f = ROOT / "apps" / app / "operator.py"
    if not f.is_file():
        return "no (console / route tool; consequential effects are staged by the core)" if TOOL_ACTIONS[app][tool] != "read" else "no"
    text = f.read_text(encoding="utf-8", errors="replace")
    m = re.search(rf"""capability\(\s*["']{re.escape(tool)}["']""", text)
    if not m:
        return "no"
    window = text[m.start():m.start() + 1400].split("capability(", 2)
    block = window[1] if len(window) > 2 else window[-1]
    return "yes" if "approval_required=True" in block else "no"


def main() -> None:
    print("# Tool classification inventory\n")
    print("The inventory behind `agentic_os/tool_actions.py` (generated by `scripts/generate_tool_inventory.py`; a test fails when a tool has no row or a row disagrees with the registry). "
          "One row per tool. **Classification is not enforcement:** the gate enforces the action recorded here, and a static scan cannot prove a tool read-only. "
          "`tests/test_read_classification_sinks.py` is a regression signal only (it cannot see indirect execution, imported helpers, or a subprocess behind an injected runner). "
          "A tool whose behaviour is selected by the caller's input (a mode flag, a playbook, target or vars argument, a scan with options) is classified at the strictest action it can perform "
          "unless it is split into separate tools. Authorisation (this table) and approval (the last column) are separate checks.\n")
    print("Categories: `read-only-state` (reads state, writes nothing observable) / `writes-local-or-persistent-artifacts` (files, assets, records it owns) / `launches external process or network` "
          "(a scanner, a deploy, a provider or CRM/e-mail/payment call, an editor service) / `dynamically selected behaviour` (the caller chooses what runs) / "
          "`public intake: unauthenticated by design` (the tool is `write` for the Operator, `/invoke` and the console, and a route registered in `PUBLIC_INTAKE_ROUTES` may ALSO call it with no grant, to open a "
          "PENDING request only; see `docs/PERMISSIONS_MIGRATION.md` section 30).\n")
    print("| tool | app | action | category | evidence | approval needed | reviewed by / date |")
    print("|---|---|---|---|---|---|---|")
    for app in sorted(TOOL_ACTIONS):
        for tool, action in sorted(TOOL_ACTIONS[app].items()):
            print(f"| `{tool}` | {app} | {action} | {category(tool, action, app)} | {evidence(app, tool)} | {approval(app, tool)} | {REVIEWER} |")


if __name__ == "__main__":
    main()
