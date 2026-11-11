"""The component-to-licence inventory (docs/legal/COMPONENT_LICENCE_INVENTORY.md) and the licence schedule in LICENSING.md.

* the committed inventory equals what scripts/build_component_licence_inventory.py generates;
* every tracked file belongs to a row of the inventory (a new directory fails until it is classified);
* every app carries a LICENSE that is byte-identical to the GNU AGPL text of the root, and no app carries the Commons Clause;
* the standard texts are the standard texts: the Commons Clause file names the Software and Licensor only in its three fill-in lines;
* the schedule in LICENSING.md names the same categories as the inventory and says that the AGPL section 7 / 10 question is open;
* LICENSE.md carries the scope-of-Software text and the voice integration's copy is byte-identical.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _gen():
    spec = importlib.util.spec_from_file_location("_inv", ROOT / "scripts" / "build_component_licence_inventory.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_inventory_is_current():
    assert _gen().main(["--check"]) == 0


def test_every_tracked_file_is_classified():
    assert _gen().unclassified() == []


def test_apps_carry_the_plain_agpl_text_and_no_commons_clause():
    root_text = (ROOT / "LICENSE").read_bytes()
    apps = sorted(p for p in (ROOT / "apps").iterdir() if p.is_dir() and not p.name.startswith("__"))
    assert len(apps) == 21
    for a in apps:
        assert (a / "LICENSE").read_bytes() == root_text, a.name
        assert not (a / "LICENSE-COMMONS-CLAUSE").exists(), a.name


def test_commons_clause_text_is_the_standard_text_with_three_fill_in_lines():
    text = (ROOT / "LICENSE-COMMONS-CLAUSE").read_text(encoding="utf-8")
    assert text.startswith('"Commons Clause" License Condition v1.0')
    fill = [ln for ln in text.splitlines() if ln.startswith(("Software:", "License:", "Licensor:"))]
    assert len(fill) == 3


def test_schedule_and_open_question_are_in_licensing_md():
    t = (ROOT / "LICENSING.md").read_text(encoding="utf-8")
    for needle in ("Schedule A", "Schedule B", "Schedule C", "Unresolved, for counsel", "sections 7 and 10", "COMPONENT_LICENCE_INVENTORY.md"):
        assert needle in t, needle
    inv = (ROOT / "docs" / "legal" / "COMPONENT_LICENCE_INVENTORY.md").read_text(encoding="utf-8")
    assert "decides no legal question" in inv and "D-LIC-3" in inv


def test_licence_md_scope_text_and_voice_copy():
    lic = (ROOT / "LICENSE.md").read_bytes()
    assert b"## Scope of the Commons Clause condition" in lic
    assert (ROOT / "integrations" / "voice-pipecat" / "LICENSE.md").read_bytes() == lic
