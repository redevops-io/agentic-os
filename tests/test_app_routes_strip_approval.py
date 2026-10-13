"""Every app route that reads a raw JSON body must strip caller-supplied `_approval*` markers first."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

APPS = Path(__file__).resolve().parents[1] / "apps"
APP_FILES = [p for p in sorted(APPS.glob("*/app.py")) if p.parent.name != "guide"]
RAW = re.compile(r"(?<!_strip_approval\()await\s+(?:request|req)\.json\(\)")


@pytest.mark.parametrize("path", APP_FILES, ids=lambda p: p.parent.name)
def test_no_raw_request_json_reaches_handlers(path):
    txt = path.read_text()
    assert not RAW.search(txt), f"{path.parent.name}: route reads request.json() without _strip_approval()"
    if "_strip_approval(await" in txt:
        assert "def _strip_approval(" in txt


@pytest.mark.parametrize("path", [p for p in APP_FILES if "def _strip_approval(" in p.read_text()],
                         ids=lambda p: p.parent.name)
def test_helper_strips_all_variants(path):
    src = path.read_text()
    start = src.index("def _strip_approval(")
    end = src.index("\n\n\n", start)
    ns: dict = {}
    exec(src[start:end], ns)  # noqa: S102 — the helper is self-contained stdlib-only
    out = ns["_strip_approval"]({"action": "x", "_approval": True, "_Approval": 1, "__approval": 1,
                                 "_APPROVAL_token": 1, "keep": {"_approval": {"a": 1}, "ok": 1},
                                 "lst": [{"_approval": 1, "v": 2}]})
    assert out == {"action": "x", "keep": {"ok": 1}, "lst": [{"v": 2}]}
