"""RR-06 / RR-14: the control-plane /permissions page keeps the admin key IN MEMORY ONLY (an input field, like the public demo page): no
localStorage, sessionStorage, IndexedDB or cookie, no stored key read back; the static page embeds only fixed constants (no store data).

Checked by RUNNING the page script in node with browser storage that throws on any access (no browser, no network).
"""
from __future__ import annotations

import base64
import html.parser
import inspect
import json
import pathlib
import shutil
import subprocess

import pytest

from agentic_os import permissions as P
from agentic_os import permissions_demo as demo_engine
from agentic_os import views

ROOT = pathlib.Path(__file__).resolve().parents[1]
needs_node = pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
STORAGE = ("localStorage", "sessionStorage", "indexedDB", "document.cookie", "caches.", "openDatabase")


def _admin_page(subjects=("a",)):
    return views.permissions_page(subjects=list(subjects), roles=["admin"], resource=P.PREVIEW_RESOURCE, columns=P.PREVIEW_COLS,
                                  subject_kinds=list(P.SUBJECT_KINDS), resource_kinds=list(P.RESOURCE_KINDS), actions=list(P.ACTIONS),
                                  row_scopes=list(P.ROW_SCOPES))


def _demo_page():
    return views.permissions_demo_page(demo_engine.list_scenarios())


def _render(page, fixtures, tmp):
    (tmp / "page.html").write_text(page)
    (tmp / "fx.json").write_text(json.dumps(fixtures))
    p = subprocess.run(["node", str(ROOT / "tests" / "_page_js_harness.js"), str(tmp / "page.html"), str(tmp / "fx.json")],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr[-500:]
    out = json.loads(p.stdout.strip().splitlines()[-1])
    assert "error" not in out, out
    return out


class _Tags(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


@pytest.mark.parametrize("make", [_admin_page, _demo_page], ids=["control-plane", "showcase-demo"])
class TestNeitherPageUsesBrowserStorageOrInlineHandlers:
    def test_the_source_has_no_storage_api(self, make):
        page = make()
        for api in STORAGE:
            assert api not in page, api

    def test_the_markup_has_no_inline_event_handler(self, make):
        p = _Tags()
        p.feed(make())
        assert [(t, a) for t, a in p.tags if any(k.startswith("on") for k in a)] == []

    def test_the_key_field_is_a_password_input_that_asks_the_browser_not_to_remember_it(self, make):
        p = _Tags()
        p.feed(make())
        keys = [a for t, a in p.tags if t == "input" and a.get("id") in ("apikey", "akey")]
        assert len(keys) == 1 and keys[0]["type"] == "password" and keys[0].get("autocomplete") == "off"


@needs_node
class TestControlPlanePage:
    FX = {"api/permissions/grants": {"status": 200, "body": []},
          "api/permissions/status": {"status": 200, "body": {"encrypted": True, "key_source": "env", "count": 0}},
          "api/permissions/preview": {"status": 200, "body": {"full": {"count": 0, "records": []}, "limited": {
              "count": 0, "withheld": 0, "records": [], "decision": {"allowed": True, "reason": "ok"}}}}}

    def test_the_page_makes_no_call_at_all_until_the_operator_unlocks_it(self, tmp_path):
        out = _render(_admin_page(), {**self.FX, "__storage_throws": True, "__values": {"apikey": "typed-but-not-applied"}}, tmp_path)
        assert out["calls"] == []

    def test_unlock_applies_the_typed_key_to_every_call_clears_the_field_and_never_touches_storage(self, tmp_path):
        out = _render(_admin_page(), {**self.FX, "__storage_throws": True, "__type": {"apikey": "typed-key"}, "__press": ["unlock"]}, tmp_path)
        assert out["calls"] and all(c["headers"].get("X-API-Key") == "typed-key" for c in out["calls"])
        assert out["els"]["apikey"]["value"] == ""                          # the key no longer sits in the DOM

    def test_typing_alone_sends_nothing_there_is_no_per_keystroke_listener(self, tmp_path):
        """PP-03: the old page called the admin API three times per keystroke with the partial key in X-API-Key."""
        key = "partial-key-DO-NOT-STORE"
        out = _render(_admin_page(), {**self.FX, "__storage_throws": True,
                                      "__keystrokes": {"apikey": [key[:i] for i in range(1, len(key) + 1)]}}, tmp_path)
        assert out["calls"] == [] and "input" not in out["els"]["apikey"]["listeners"] and "change" not in out["els"]["apikey"]["listeners"]

    def test_a_key_left_in_storage_by_the_old_page_is_never_read_back_or_sent(self, tmp_path):
        out = _render(_admin_page(), {**self.FX, "__ls": {"cp_api_key": "OLD-STORED-KEY"}, "__values": {"apikey": ""}, "__press": ["unlock"]}, tmp_path)
        assert out["calls"] == [] and "OLD-STORED-KEY" not in json.dumps(out["els"])

    def test_the_key_is_never_in_a_url_or_a_body(self, tmp_path):
        out = _render(_admin_page(), {**self.FX, "__type": {"apikey": "secret-key-xyz"}, "__press": ["unlock"]}, tmp_path)
        assert not any("secret-key-xyz" in c["url"] for c in out["calls"])

    def test_pressing_unlock_with_an_empty_field_does_nothing(self, tmp_path):
        assert _render(_admin_page(), {**self.FX, "__press": ["unlock"]}, tmp_path)["calls"] == []

    def test_lock_forgets_the_key(self, tmp_path):
        out = _render(_admin_page(), {**self.FX, "__type": {"apikey": "k" * 30}, "__press": ["unlock", "lock", "addbtn"]}, tmp_path)
        n = len(out["calls"])
        assert n and out["els"]["pstatus"]["textContent"] == "locked"
        assert out["els"]["grows"]["innerHTML"].count("locked") == 1


@needs_node
def test_the_demo_pages_admin_panel_never_touches_storage_either(tmp_path):
    fx = {"__storage_throws": True, "__values": {"akey": "k" * 30, "sc": "support-us-masked"}, "__press": ["aload"],
          "api/permissions/grants": {"status": 200, "body": []}, "api/permissions/status": {"status": 200, "body": {"encrypted": True}},
          "api/demo/scenarios/support-us-masked": {"status": 200, "body": demo_engine.evaluate("support-us-masked")}}
    out = _render(_demo_page(), fx, tmp_path)
    assert [c for c in out["calls"] if "api/permissions" in c["url"]]


# ───────────────────────── RR-14: what the public static page embeds ─────────────────────────

@pytest.fixture()
def cp(tmp_path, monkeypatch):
    monkeypatch.setenv("PERMISSIONS_KEY", base64.b64encode(b"l" * 32).decode())
    monkeypatch.setenv("PERMISSIONS_DIR", str(tmp_path / "perm"))
    monkeypatch.setenv("AGENTIC_OS_HOME", str(tmp_path / "home"))
    from agentic_os import control_plane
    return control_plane


def test_the_static_page_handler_reads_no_store_and_no_request_data(cp):
    """The anonymous page is built from constants only: its handler never touches the grant store, the request or the environment."""
    import ast
    tree = ast.parse(inspect.getsource(cp.permissions_page).lstrip())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not names & {"_GRANTS", "request", "Request", "environ", "getenv", "headers", "grants", "status"}, names
    assert [a.arg for a in tree.body[0].args.args] == []                      # takes no request parameter


def test_the_embedded_option_values_are_exactly_the_fixed_allowlist(cp):
    modules = {m for members in cp.GROUPS.values() for m in members}
    page = cp.permissions_page().body.decode()
    p = _Tags()
    p.feed(page)
    options = {a["value"] for t, a in p.tags if t == "option" and "value" in a}
    expected = set(P.SUBJECT_KINDS) | set(P.RESOURCE_KINDS) | set(P.ROW_SCOPES) | modules | \
        {"admin", "finance", "support", "analyst", "auditor"} | set(P.PREVIEW_RESOURCE["row_columns"])
    assert options <= expected, sorted(options - expected)
    assert modules <= options          # the module names are the repository's own constants (cp.GROUPS), nothing deployment-specific
