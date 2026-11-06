"""Projects APP DIRECTORY: Open App / Open Record / Inspect Execution from CONFIGURED application routes.

The directory only ever PRODUCES LINKS. It never proxies, never forwards a credential, a session or a
launch code, and never redirects. Every URL comes from operator-supplied configuration (no port or host is
written in code) and every record value is validated and percent-encoded before it is placed in a template.
What a caller may see is computed server-side from its permission ceiling (the identity plane's
``authorize``); an app or record outside the ceiling is indistinguishable from one that does not exist.
"""
from __future__ import annotations

import copy
import json

import pytest

from agentic_os.app_directory import (AppDirectory, DirectoryConfigError, DirectoryNotFound,
                                      GrantsCeiling, IdentityCeiling, load_directory, directory_from_env)
from agentic_os.overlays import LocalIdentity, Principal

ALICE = Principal("alice", "user", ("operator",))


class FakeCeiling:
    """Allows exactly the permissions it is given (deny by default), like the identity plane."""
    def __init__(self, *perms):
        self.perms = set(perms)
        self.asked = []

    def allows(self, principal, permission):
        self.asked.append((principal.id, permission))
        return permission in self.perms


ALL = ("apps.support.open", "apps.support.records.conversation", "apps.support.inspect",
       "apps.billing.open", "apps.billing.records.invoice",
       "apps.books.open", "apps.infra.open")

BASE = {
    "policy": {"allowed_schemes": ["https"], "allow_loopback_http": False},
    "apps": [
        {"id": "support", "display_name": "Support", "tagline": "Front-line support.",
         "ui": "public-to-user", "ui_url": "https://support.example.com/app", "auth": "app-login",
         "records": {"conversation": {
             "label": "Conversation",
             "template": "/app/accounts/{account_id}/conversations/{conversation_id}",
             "params": {"account_id": "^[0-9]{1,10}$", "conversation_id": "^[0-9]{1,12}$"},
             "ref_pattern": "^conversation:(?P<account_id>[0-9]+):(?P<conversation_id>[0-9]+)$"}},
         "inspect": {"template": "/app/executions/{mission_id}", "params": {"mission_id": "^[A-Za-z0-9_-]{1,64}$"}},
         "bindings": {"support.resolve": {"kind": "conversation",
                                          "params": {"account_id": "=1", "conversation_id": "result.conversation_id"}}}},
        {"id": "billing", "display_name": "Billing", "ui": "public-to-user", "auth": "sso",
         "ui_url": "https://billing.example.com/", "records": {"invoice": {
             "template": "/invoices/{invoice_id}", "params": {"invoice_id": "^inv_[A-Za-z0-9]{4,32}$"}}}},
        {"id": "books", "display_name": "Books", "ui": "public-to-user", "auth": "app-login",
         "ui_url": "/m/books"},                                  # same-origin, no record view, no inspect view
        {"id": "infra", "display_name": "Infra", "ui": "none"},                # API-only operator
        {"id": "vault", "display_name": "Vault", "ui": "internal", "ui_url": "https://vault.example.com/"},
        {"id": "nolink", "display_name": "No link yet", "ui": "public-to-user", "auth": "app-login"},  # UI promised, no route configured
    ],
}


def cfg(**over):
    c = copy.deepcopy(BASE)
    c.update(over)
    return c


def d(config=None, **kw):
    return load_directory(config if config is not None else cfg(), **kw)


def app_cfg(**over):
    """One extra public app with the given overrides, appended to an otherwise valid config."""
    a = {"id": "extra", "display_name": "Extra", "ui": "public-to-user", "auth": "app-login",
         "ui_url": "https://extra.example.com/"}
    a.update(over)
    c = cfg()
    c["apps"] = [a]
    return c


# ───────────────────────── listing and the permission ceiling ─────────────────────────
def test_listing_only_public_to_user_apps_within_the_ceiling():
    out = d().listing(ALICE, FakeCeiling(*ALL))
    ids = [a["id"] for a in out]
    assert ids == ["support", "billing", "books"] or set(ids) == {"support", "billing", "books"}
    assert "infra" not in ids and "vault" not in ids          # ui none / internal never appear, even with the permission


def test_ceiling_hides_apps_entirely_no_marker():
    out = d().listing(ALICE, FakeCeiling("apps.books.open"))
    assert [a["id"] for a in out] == ["books"]
    blob = json.dumps(out)
    assert "support.example.com" not in blob and "billing.example.com" not in blob and "Support" not in blob


def test_deny_by_default_for_an_empty_ceiling():
    assert d().listing(ALICE, FakeCeiling()) == []


def test_identity_ceiling_uses_the_identity_planes_authorize(monkeypatch):
    import agentic_os.overlays as ov
    monkeypatch.setattr(ov, "_IDENTITY", LocalIdentity(grants={"apps.books.open"}))
    out = d().listing(ALICE, IdentityCeiling())
    assert [a["id"] for a in out] == ["books"]


def test_grants_ceiling_maps_the_grant_store(tmp_path, monkeypatch):
    monkeypatch.setenv("PERMISSIONS_KEY", "")
    from agentic_os.permissions import GrantStore, PermissionsPlane
    store = GrantStore(str(tmp_path))
    store.add("user", "alice", "tool", "apps.books", actions=["read"])        # a dotted prefix covers apps.books.open
    c = GrantsCeiling(PermissionsPlane(store))
    out = d().listing(Principal("alice", "user", ()), c)
    assert [a["id"] for a in out] == ["books"]
    assert d().listing(Principal("mallory", "user", ()), c) == []


def test_sealed_grant_store_fails_closed(tmp_path):
    from agentic_os.permissions import GrantStore, PermissionsPlane
    store = GrantStore(str(tmp_path))
    store.add("user", "alice", "tool", "apps", actions=["read"])
    store.path.write_bytes(b"tampered")                                      # unreadable -> sealed -> deny everything
    assert d().listing(ALICE, GrantsCeiling(PermissionsPlane(store))) == []


def test_open_app_url_comes_only_from_configuration():
    s = {a["id"]: a for a in d().listing(ALICE, FakeCeiling(*ALL))}
    assert s["support"]["open"] == {"available": True, "url": "https://support.example.com/app"}
    assert s["books"]["open"] == {"available": True, "url": "/m/books"}      # origin-relative, resolved by the browser


def test_public_app_without_a_configured_route_is_listed_not_available_with_a_reason():
    out = {a["id"]: a for a in d().listing(ALICE, FakeCeiling("apps.nolink.open"))}
    o = out["nolink"]["open"]
    assert o["available"] is False and "url" not in o and o["reason"]


def test_unsupported_record_and_inspect_are_marked_not_available_with_reasons():
    s = {a["id"]: a for a in d().listing(ALICE, FakeCeiling(*ALL))}
    assert s["books"]["records"]["available"] is False and s["books"]["records"]["reason"]
    assert s["books"]["inspect"]["available"] is False and s["books"]["inspect"]["reason"]
    assert s["support"]["records"] == {"available": True, "kinds": [{"kind": "conversation", "label": "Conversation"}]}
    assert s["support"]["inspect"]["available"] is True


def test_record_kind_the_user_may_not_open_is_not_available_but_the_app_still_lists():
    s = {a["id"]: a for a in d().listing(ALICE, FakeCeiling("apps.support.open"))}["support"]
    assert s["records"]["available"] is False and s["records"]["reason"]
    assert s["inspect"]["available"] is False


def test_listing_exposes_only_whitelisted_fields_never_internal_urls():
    out = d().listing(ALICE, FakeCeiling(*ALL))
    for a in out:
        assert set(a) <= {"id", "display_name", "tagline", "auth", "open", "records", "inspect"}


def test_internal_service_fields_cannot_be_carried_in_config():
    for key in ("service_url", "api_url", "internal_url", "proxy"):
        dd = d(app_cfg(**{key: "http://billing:8201"}))
        assert dd.apps == {}                                                   # the entry is refused, not half-loaded
        assert any("unknown key" in e and key in e for e in dd.errors)


def test_auth_note_never_claims_projects_signs_the_user_in():
    out = d().listing(ALICE, FakeCeiling(*ALL))
    for a in out:
        low = a["auth"].lower()
        assert "launch code" not in low and "token" not in low and "session" not in low
        assert "own" in low or "sign" in low or "single" in low


def test_listing_asks_the_ceiling_with_stable_permission_names():
    c = FakeCeiling()
    d().listing(ALICE, c)
    assert ("alice", "apps.support.open") in c.asked and ("alice", "apps.books.open") in c.asked
    assert not any(p.startswith("apps.infra") or p.startswith("apps.vault") for _u, p in c.asked)


# ───────────────────────── config validation: Open App URLs ─────────────────────────
@pytest.mark.parametrize("bad", [
    "javascript:alert(1)", "JavaScript:alert(1)", " javascript:alert(1)", "data:text/html,<script>1</script>",
    "file:///etc/passwd", "ftp://files.example.com/", "vbscript:x", "blob:https://x.example.com/1", "about:blank",
    "//evil.example/", "///evil.example", "\\\\evil.example\\x", "https:evil.example",
    "http://support.example.com/",                        # plain http to a non-loopback host
    "https://user:pw@support.example.com/",               # credentials in the URL
    "https://good.example.com@evil.example/",             # userinfo trick
    "https://support.example.com/a b", "https://support.example.com/\n", "https://support.example.com/\x00",
    "https://support.example.com/#frag", "https://support.example.com/x\\y",
    "https://support.example.com/%2e%2e/admin", "https://support.example.com/a/../b", "https://support.example.com/a%2fb",
    "https://billing/", "https://billing:8201/",          # single-label host: a compose-internal service name
    "https://exa mple.com/", "https://münchen.example.com/", "https://support.example.com:99999/", "https://support.example.com:0/",
    "https://", "https:///x", "", "   ", None, 7, ["https://x.example.com/"],
    "/\\evil.example", "/m/x/../../y", "/m/x?y=1#z", "m/books",
])
def test_unsafe_ui_url_refuses_the_app(bad):
    dd = d(app_cfg(ui_url=bad))
    assert dd.apps == {}
    assert dd.errors, f"expected a config error for {bad!r}"
    assert dd.listing(ALICE, FakeCeiling("apps.extra.open")) == []


def test_http_is_accepted_only_for_loopback_and_only_when_the_policy_allows_it():
    c = app_cfg(ui_url="http://127.0.0.1:9999/")
    assert d(c).apps == {}                                                     # default policy: https only
    c["policy"] = {"allowed_schemes": ["https"], "allow_loopback_http": True}
    assert "extra" in d(c).apps
    c2 = app_cfg(ui_url="http://evil.example.com/")
    c2["policy"] = {"allowed_schemes": ["https"], "allow_loopback_http": True}
    assert d(c2).apps == {}                                                    # loopback exemption is not a plain-http exemption
    for loop in ("http://localhost:3003/", "http://[::1]:3003/"):
        c3 = app_cfg(ui_url=loop)
        c3["policy"] = {"allowed_schemes": ["https"], "allow_loopback_http": True}
        assert "extra" in d(c3).apps


def test_policy_cannot_allow_dangerous_schemes():
    for scheme in ("javascript", "data", "file", "ftp", "blob", "vbscript"):
        with pytest.raises(DirectoryConfigError):
            d(cfg(policy={"allowed_schemes": [scheme]}))


def test_allowed_hosts_policy_pins_every_app_to_a_known_domain():
    c = cfg(policy={"allowed_schemes": ["https"], "allowed_hosts": ["example.com", "*.example.com"]})
    assert "support" in d(c).apps
    c = cfg(policy={"allowed_schemes": ["https"], "allowed_hosts": ["corp.example.org"]})
    dd = d(c)
    assert "support" not in dd.apps and dd.errors
    c = app_cfg(ui_url="https://evilexample.com/")
    c["policy"] = {"allowed_schemes": ["https"], "allowed_hosts": ["*.example.com"]}
    assert d(c).apps == {}                                                     # suffix match is on a label boundary


def test_one_bad_entry_does_not_drop_the_others_and_errors_are_not_in_the_listing():
    c = cfg()
    c["apps"].append({"id": "evil", "display_name": "E", "ui": "public-to-user", "ui_url": "javascript:alert(1)"})
    dd = d(c)
    assert "evil" not in dd.apps and "support" in dd.apps and dd.errors
    assert "javascript" not in json.dumps(dd.listing(ALICE, FakeCeiling("apps.evil.open", *ALL)))


def test_duplicate_and_malformed_ids_are_refused():
    c = cfg()
    c["apps"].append(copy.deepcopy(c["apps"][0]))
    assert d(c).errors
    for bad in ("Support", "a b", "../x", "a/b", "", "x" * 80, "ünï"):
        assert d(app_cfg(id=bad)).apps == {}


def test_ui_must_be_one_of_the_known_values():
    assert d(app_cfg(ui="public")).apps == {}                                  # not "public-to-user": refused, not guessed
    assert d(app_cfg(ui="public-to-user")).apps != {}


def test_malformed_top_level_config_is_an_error_not_a_default_open_directory():
    for bad in ([], "x", {"apps": "nope"}, {"apps": [1]}, {"policy": []}, {"apps": [], "extra_top": 1}):
        with pytest.raises(DirectoryConfigError):
            load_directory(bad)


# ───────────────────────── config validation: record templates ─────────────────────────
@pytest.mark.parametrize("tpl", [
    "https://evil.example/{id}", "//evil.example/{id}", "{id}", "{host}/x", "records/{id}",
    "/r/{undeclared}", "/r/{id", "/r/id}", "/r/{}", "/r/{Id}", "/r/{id}{id2}x{", "/r/{id!r}", "/r/{id.attr}", "/r/{id[0]}",
    "/r/%2e%2e/{id}", "/r/../{id}", "/r/{id}#frag", "/r/\\{id}", "/r/ {id}", "/r/{id}\n",
    "/other-app/{id}",                                   # outside the app's own base path
])
def test_unsafe_record_templates_refuse_the_app(tpl):
    c = app_cfg(ui_url="https://extra.example.com/app",
                records={"thing": {"template": tpl, "params": {"id": "^[0-9]{1,6}$"}}})
    dd = d(c)
    assert dd.apps == {}
    assert dd.errors


def test_permissive_param_patterns_are_refused():
    for pat in (".*", ".+", "^.*$", "^[^/]+$", r"^\S+$", "[0-9]+", "^[0-9]+", "[0-9]+$", r"^[\w/.-]+$", "^[a-z./]{1,9}$", "^(a|/)$",
                "^[0-9]{1,6}", "(", "^[\u0000-￿]+$"):
        c = app_cfg(records={"thing": {"template": "/r/{id}", "params": {"id": pat}}})
        assert d(c).apps == {}, pat


def test_params_must_be_a_string_pattern_and_every_placeholder_declared():
    for params in ({}, {"id": 5}, {"id": None}, {"id": ["^a$"]}, {"id": {"p": "^a$"}}):
        c = app_cfg(records={"thing": {"template": "/r/{id}", "params": params}})
        assert d(c).apps == {}


def test_template_in_the_apps_own_path_prefix_is_accepted():
    c = app_cfg(ui_url="https://extra.example.com/app",
                records={"thing": {"template": "/app/things/{id}?view=full", "params": {"id": "^[0-9]{1,6}$"}}})
    assert "extra" in d(c).apps


# ───────────────────────── record links ─────────────────────────
def link(dd, ceiling=None, app="support", kind="conversation", params=None):
    return dd.record_link(ALICE, ceiling or FakeCeiling(*ALL), app, kind,
                          params if params is not None else {"account_id": "1", "conversation_id": "42"})


def test_record_link_renders_the_template_with_validated_values():
    r = link(d())
    assert r == {"app": "support", "kind": "conversation", "available": True,
                 "url": "https://support.example.com/app/accounts/1/conversations/42", "label": "Conversation"}


def test_record_link_accepts_integer_ids_from_json():
    assert link(d(), params={"account_id": 1, "conversation_id": 42})["url"].endswith("/accounts/1/conversations/42")


@pytest.mark.parametrize("val", [
    "../../etc/passwd", "1/../2", "..", ".", "1%2f..%2f2", "%2e%2e", "1/2", "1\\2", "1?x=1", "1#x", "1 2", "1\n2", "1\x002",
    "", " ", "-1", "+1", "1.5", "１２", "٤٢", "1" * 40, "javascript:alert(1)", "https://evil.example/", "//evil.example",
    "42@evil.example", "42;x", "42&x=1", "0x2a", "{conversation_id}", "${x}", "{{7*7}}", None, True, 1.5, [], {}, ["42"], {"a": 1},
])
def test_record_value_injection_is_refused(val):
    with pytest.raises(ValueError) as e:
        link(d(), params={"account_id": "1", "conversation_id": val})
    assert not isinstance(e.value, DirectoryNotFound)
    msg = str(e.value)
    assert "evil" not in msg and "passwd" not in msg and "alert" not in msg      # the offending value is never echoed back


def test_record_link_requires_exactly_the_declared_params():
    with pytest.raises(ValueError):
        link(d(), params={"account_id": "1"})
    with pytest.raises(ValueError):
        link(d(), params={"account_id": "1", "conversation_id": "42", "host": "evil.example"})
    with pytest.raises(ValueError):
        link(d(), params={"account_id": "1", "conversation_id": "42", "url": "https://evil.example/"})


def test_a_caller_supplied_url_or_host_is_never_used():
    dd = d()
    r = dd.record_link(ALICE, FakeCeiling(*ALL), "support", "conversation",
                       {"account_id": "1", "conversation_id": "42"})
    assert r["url"].startswith("https://support.example.com/")
    for forged in ("https://evil.example/x", "//evil.example/x", "/m/other"):
        with pytest.raises(ValueError):
            dd.record_link(ALICE, FakeCeiling(*ALL), "support", "conversation",
                           {"account_id": "1", "conversation_id": forged})


def test_record_link_for_an_app_outside_the_ceiling_is_indistinguishable_from_unknown():
    dd = d()
    errs = []
    for app, perms in (("support", ()), ("nosuchapp", ALL), ("infra", ALL), ("vault", ALL)):
        with pytest.raises(DirectoryNotFound) as e:
            dd.record_link(ALICE, FakeCeiling(*perms), app, "conversation", {"account_id": "1", "conversation_id": "42"})
        errs.append(str(e.value))
    assert len(set(errs)) == 1                                                  # one message for every case


def test_record_link_for_a_kind_without_permission_or_support_is_not_available_with_reason():
    dd = d()
    r = dd.record_link(ALICE, FakeCeiling("apps.support.open"), "support", "conversation", {"account_id": "1", "conversation_id": "42"})
    assert r["available"] is False and "url" not in r and r["reason"]
    r = dd.record_link(ALICE, FakeCeiling(*ALL), "books", "anything", {})
    assert r["available"] is False and "url" not in r and r["reason"]
    r = dd.record_link(ALICE, FakeCeiling(*ALL), "support", "no-such-kind", {})
    assert r["available"] is False and "url" not in r


def test_record_kind_names_are_not_template_input():
    for kind in ("../x", "conversation/../../x", "", "a b", None):
        r = d().record_link(ALICE, FakeCeiling(*ALL), "support", kind, {})
        assert r["available"] is False and "url" not in r


def test_final_url_is_rechecked_against_the_apps_origin(monkeypatch):
    dd = d()
    entry = dd.apps["support"]
    from agentic_os import app_directory as ad
    monkeypatch.setattr(ad, "_quote", lambda v: "x/../../../evil")             # a bug in value encoding must still be caught
    with pytest.raises(Exception):
        dd.record_link(ALICE, FakeCeiling(*ALL), "support", "conversation", {"account_id": "1", "conversation_id": "42"})
    assert entry.origin == "https://support.example.com"


# ───────────────────────── references from receipts ─────────────────────────
def test_ref_string_resolves_through_the_apps_own_ref_pattern():
    r = d().ref_link(ALICE, FakeCeiling(*ALL), "support:conversation:1:42")
    assert r["available"] is True and r["url"].endswith("/accounts/1/conversations/42")


@pytest.mark.parametrize("ref", ["", "support", "support:", ":conversation:1:42", "support:conversation:1:42/../../x",
                                 "support:conversation:1:42:extra", "support:conversation:a:b", "SUPPORT:conversation:1:42",
                                 "support:conversation:1:" + "9" * 40, "support:\nconversation:1:42", None, 5, "mission:4821",
                                 "postgres:customers#0", "https://evil.example/", "javascript:alert(1)"])
def test_malformed_refs_never_produce_a_url(ref):
    try:
        r = d().ref_link(ALICE, FakeCeiling(*ALL), ref)
    except (ValueError, DirectoryNotFound):
        return
    assert r["available"] is False and "url" not in r


def test_ref_to_an_app_outside_the_ceiling_is_not_found():
    with pytest.raises(DirectoryNotFound):
        d().ref_link(ALICE, FakeCeiling("apps.books.open"), "support:conversation:1:42")


def test_receipt_links_use_bindings_and_the_ceiling():
    receipt = {"mission_id": "m-1", "steps": [
        {"node_id": "n1", "capability": "support.resolve", "result": {"conversation_id": 42, "ticket": "t"}},
        {"node_id": "n2", "capability": "billing.invoice.create", "result": {"invoice_id": "inv_ab12"}},
        {"node_id": "n3", "capability": "unknown.thing", "result": {"conversation_id": 9}},
        {"node_id": "n4", "capability": "support.resolve", "result": {"conversation_id": "../../x"}},
    ]}
    dd = d()
    out = dd.links_for_receipt(ALICE, FakeCeiling(*ALL), receipt)
    urls = [(x["app"], x.get("url")) for x in out["records"]]
    assert ("support", "https://support.example.com/app/accounts/1/conversations/42") in urls
    assert all("evil" not in (u or "") and ".." not in (u or "") for _a, u in urls)
    n4 = [x for x in out["records"] if x["node_id"] == "n4"]
    assert n4 and n4[0]["available"] is False and "url" not in n4[0]            # malformed result value: no link, with a reason
    assert out["inspect"][0]["app"] == "support" and out["inspect"][0]["url"] == "https://support.example.com/app/executions/m-1"
    # a user whose ceiling excludes support sees nothing from support, and no hint that it exists
    out2 = dd.links_for_receipt(ALICE, FakeCeiling("apps.books.open"), receipt)
    assert out2["records"] == [] and out2["inspect"] == []
    assert "support" not in json.dumps(out2)


def test_receipt_mission_id_is_validated_before_it_enters_the_inspect_template():
    for mid in ("../../x", "m 1", "m/1", "x" * 200, "m?x=1", "%2e%2e"):
        out = d().links_for_receipt(ALICE, FakeCeiling(*ALL), {"mission_id": mid, "steps": [{"capability": "support.resolve", "result": {"conversation_id": 1}}]})
        assert out["inspect"] and all(x["available"] is False and "url" not in x for x in out["inspect"])


def test_receipt_garbage_is_tolerated():
    for bad in (None, 5, "x", [], {"steps": "x"}, {"steps": [None, 3, {"capability": 5}, {"capability": "support.resolve", "result": "x"}]}):
        out = d().links_for_receipt(ALICE, FakeCeiling(*ALL), bad)
        assert isinstance(out["records"], list) and isinstance(out["inspect"], list)


# ───────────────────────── module catalog and environment ─────────────────────────
def test_module_key_takes_display_text_from_the_module_catalog():
    c = app_cfg(module="agentic-support")
    c["apps"][0].pop("display_name")
    dd = d(c)
    from agentic_os.registry import Registry
    m = Registry.load().get("agentic-support")
    assert dd.apps["extra"].display_name == "agentic-support" and dd.apps["extra"].tagline == m.tagline


def test_unknown_module_is_a_config_error_for_that_entry():
    dd = d(app_cfg(module="no-such-module"))
    assert dd.apps == {} and dd.errors


def test_environment_loader_reads_a_file_and_is_empty_when_unset(tmp_path):
    assert directory_from_env({}).apps == {}
    p = tmp_path / "apps.json"
    p.write_text(json.dumps(cfg()))
    assert "support" in directory_from_env({"AGENTIC_OS_APP_DIRECTORY": str(p)}).apps
    y = tmp_path / "apps.yaml"
    import yaml
    y.write_text(yaml.safe_dump(cfg()))
    assert "support" in directory_from_env({"AGENTIC_OS_APP_DIRECTORY": str(y)}).apps


def test_environment_loader_failures_are_closed(tmp_path):
    assert directory_from_env({"AGENTIC_OS_APP_DIRECTORY": str(tmp_path / "missing.yaml")}).apps == {}
    bad = tmp_path / "bad.yaml"
    bad.write_text("apps: [unclosed")
    dd = directory_from_env({"AGENTIC_OS_APP_DIRECTORY": str(bad)})
    assert dd.apps == {} and dd.errors


def test_no_port_or_host_is_written_in_the_module():
    import re
    from pathlib import Path
    import agentic_os.app_directory as ad
    src = Path(ad.__file__).read_text()
    assert not re.search(r"localhost:\d|127\.0\.0\.1:\d|https?://[a-z0-9.-]+\.[a-z]{2,}", src.replace("https://example", ""))
