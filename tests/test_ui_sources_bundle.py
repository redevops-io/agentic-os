"""Static verification of the SHIPPED Projects UI bundle for the refused-registration messages and the Remove control (no network, no browser).

The UI source repository for the pinned artifact (local+ea9e53e5) no longer exists, so nothing here rebuilds or reads source: every check reads
the committed artifact (``packaging/ui-artifacts/projects-ui-root-ea9e53e5.tgz``), the lock and, when present, the unpacked copy in
``agentic_os/projects_ui``. The sentence table is DERIVED from the bundle's own JavaScript; ``GOLDEN`` is the reviewed table (a change to a
sentence is a deliberate edit of this file). The checker functions are exercised against mutated copies of the bundle so that a missing code,
a missing sentence, a ``window.confirm`` or a server message rendered as HTML makes them fail.

Server contract: ``docs/DESKTOP_SOURCE_REGISTRATION.md`` (refusal codes of /api/sources/propose|confirm|remove, 401, 503 audit_unavailable, 404
source_not_found, the 422 list shape).
"""
from __future__ import annotations

import hashlib
import json
import re
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LOCK = json.loads((ROOT / "packaging" / "projects_ui.lock.json").read_text(encoding="utf-8"))
ART = ROOT / LOCK["local_path"]

# the 14 refusal codes of the folder policy (docs/DESKTOP_SOURCE_REGISTRATION.md "Which folders") ...
REFUSAL_CODES = ["invalid_path", "not_absolute", "device_path", "unc_admin_share", "drive_root", "share_root", "filesystem_root", "home_directory",
                 "protected_location", "contains_protected_location", "not_found", "not_a_directory", "outside_allowed_roots", "not_proposed"]
# ... and the other outcomes the server (or its gate) can answer
OTHER_CODES = ["auth_required", "session_required", "audit_unavailable", "source_not_found"]
# every code must have a DISTINCT sentence; those two groups below are the reviewed table, verbatim from the bundle (``${we(e)}`` is the
# bundle's own template call: "nothing was registered" for confirm, "nothing was removed" for remove, "nothing was changed" otherwise)
GOLDEN = {
    "invalid_path": "That is not a usable folder path. Remove any * or ? and any odd characters, then try again.",
    "not_absolute": "Enter the full path of the folder, starting from the top, for example /Users/you/Documents/contracts or C:\\Users\\you\\Documents\\contracts.",
    "device_path": "That is a device path, not a folder, so it can't be connected. Pick an ordinary folder.",
    "unc_admin_share": "Administrative network shares can't be connected. Pick a folder on an ordinary share.",
    "drive_root": "A whole drive can't be connected. Pick a folder inside it.",
    "share_root": "A whole network share can't be connected. Pick a folder inside it.",
    "filesystem_root": "The root of the file system can't be connected. Pick a folder inside it.",
    "home_directory": "Your whole home folder can't be connected. Pick a folder inside it, for example Documents.",
    "protected_location": "That folder holds credentials, browser data, system files or this app's own data, so it can't be connected.",
    "contains_protected_location": "That folder contains credentials, browser data, system files or this app's own data, so it can't be connected. Pick a more specific folder.",
    "not_found": "That folder does not exist or can't be read. Check the path and try again.",
    "not_a_directory": "That path is a file, not a folder. Pick the folder that holds it.",
    "outside_allowed_roots": "That folder is outside the folders this deployment allows. Pick one inside them.",
    "not_proposed": "Only a folder you picked in a proposal can be connected. Ask Sidekick to use it as context, then choose Looks right, connect.",
    "audit_unavailable": ("The removal could not be recorded, so the folder is still registered. Try again in a moment.",
                          "The registration could not be recorded, so nothing was registered. Try again in a moment."),
    "session_required": "This app session has ended, so ${we(e)}. Reopen the app to start a new one.",
    "auth_required": "You're signed out of this app session, so ${we(e)}. Reopen the app to sign in again.",
    "source_not_found": ("That source is already gone, so there is nothing to remove. The list has been refreshed.",
                         "That source is no longer registered. Reload the list and try again."),
    "network": "Can't reach the runtime, so ${we(e)}. Check that the app is still running, then try again.",
    "timeout": "The runtime took too long to answer, so ${we(e)}. Try again in a moment.",
    "invalid_request": "That is not a valid source description, so ${we(e)}. Check the details and try again.",
}
# status-based sentences for a code the table does not know (the generic fallback) and for a failure with no status
FALLBACKS = ["Something went wrong on the runtime, so ${we(t)}. Try again in a moment.",
             "The runtime refused this request, so ${we(t)}. Check the folder and try again.",
             "Something went wrong, so ${we(t)}. Try again in a moment."]


def _members(path: Path = ART) -> dict[str, bytes]:
    with tarfile.open(path, "r:gz") as t:
        return {m.name: t.extractfile(m).read() for m in t.getmembers() if m.isfile()}


def _js(members) -> str:
    return next(v for k, v in members.items() if k.endswith(".js")).decode("utf-8")


def _css(members) -> str:
    return next(v for k, v in members.items() if k.endswith(".css")).decode("utf-8")


# ------------------------------------------------------------------------------------------------------------------ derivation from the bundle
_STR = r'"(?:[^"\\]|\\.)*"'
_ENTRY = re.compile(r'(?P<code>[a-z_]+):(?P<val>Pe\((?P<s>' + _STR + r')\)|e=>(?:`(?P<t>[^`]*)`|e==="remove"\?(?P<r>' + _STR + r'):(?P<o>' + _STR + r')))')


def sentence_table(js: str) -> dict:
    """code -> sentence (a string, or a (remove, other) pair), read out of the bundle's ``wi={...}`` table."""
    j = js.find("},Xy=[")
    i = js.rfind("wi={", 0, j)
    assert 0 < i < j, "the sentence table was not found in the bundle"
    out = {}
    for m in _ENTRY.finditer(js[i + 4:j + 1]):
        if m.group("s") is not None:
            out[m.group("code")] = json.loads(m.group("s"))
        elif m.group("t") is not None:
            out[m.group("code")] = m.group("t")
        else:
            out[m.group("code")] = (json.loads(m.group("r")), json.loads(m.group("o")))
    return out


def problems(js: str, css: str = "") -> list[str]:
    """Everything the shipped bundle must satisfy; an empty list is a pass. Mutation tests below break one thing at a time."""
    out: list[str] = []
    try:
        table = sentence_table(js)
    except AssertionError as e:
        return [str(e)]
    for code in REFUSAL_CODES + OTHER_CODES:
        if code not in table:
            out.append(f"no sentence for {code}")
        elif table[code] != GOLDEN[code]:
            out.append(f"the sentence for {code} changed")
    flat = [s for v in table.values() for s in (v if isinstance(v, tuple) else (v,))]
    if len(flat) != len(set(flat)):
        out.append("two codes share one sentence")
    m = re.search(r'Xy=(\[[^\]]*\])', js)
    if not m or json.loads(m.group(1)) != REFUSAL_CODES:
        out.append("the list of codes that name the folder is not the 14 refusal codes")
    for s in FALLBACKS:
        if s not in js:
            out.append("a generic fallback sentence is missing: " + s)
    # the sentences are chosen by the server's CODE only; the error object's message/detail are never read by the mapper nor rendered by the alert
    a, b = js.find("function ki("), js.find("const ad=[{id:\"chat\"")
    mapper = js[a:b]
    if not mapper or ".message" in mapper or ".detail" in mapper or "dangerouslySetInnerHTML" in mapper:
        out.append("the mapper reads the server's message")
    alert = js[js.find("function Ao("):js.find("const we=")]
    if 'role:"alert"' not in alert or "e.title" not in alert or "e.sentence" not in alert or "e.path" not in alert or ".message" in alert or "innerHTML" in alert.lower():
        out.append("the alert component is not role=alert built from title/sentence/path only")
    if '"Dismiss"' not in alert or "onDismiss" not in alert:
        out.append("the alert cannot be dismissed")
    # no browser dialogs, no raw HTML from the app (React's own internals mention dangerouslySetInnerHTML; an app use would be an object literal)
    if re.search(r"window\.confirm|[^\w.]confirm\(|[^\w.]alert\(|window\.alert|window\.prompt|[^\w.]prompt\(", js):
        out.append("a browser dialog is used")
    if re.search(r"dangerouslySetInnerHTML:\s*\{|\{\s*__html\s*:", js):
        out.append("the app sets raw HTML")
    if 'outline:"none"' in js or "outlineStyle" in js or re.search(r"outline:\s*none", js.replace("\\n", " ")):
        out.append("an inline outline removal")
    # server error text is not what the page shows: these two server wordings must not appear
    for words in ("cannot be registered as a source", "A local session is required."):
        if words in js:
            out.append("the server's own wording is in the bundle: " + words)
    # Remove: accessible name, in-page confirmation (Escape), the route
    if not re.search(r'"aria-label":`Remove \$\{y\}\?`,onKeyDown:(\w)=>\{\1\.key==="Escape"&&\(\1\.stopPropagation\(\),\w\(\)\)', js):
        out.append("Remove control: missing Escape handler")
    for needle in ('"aria-label":`Remove ${y}`', '"aria-expanded":i', "Yes, remove", "Keep", "/api/sources/remove",
                   'role:"group","aria-label":`Remove ${y}?`', 'className:"s removed-note",role:"status"'):
        if needle not in js:
            out.append("Remove control: missing " + needle)
    if css:
        for rule in (".problem{", ".confirm{", ".removed-note{"):
            if not re.search(r"(^|\})" + re.escape(rule), css):
                out.append("css rule missing " + rule)
        if ":focus-visible{outline:var(--focus-w) solid var(--focus)" not in css:
            out.append("shared focus ring missing")
    return out


# ------------------------------------------------------------------------------------------------------------------ the shipped artifact
def test_the_artifact_hash_and_every_member_hash_equal_the_lock():
    assert hashlib.sha256(ART.read_bytes()).hexdigest() == LOCK["sha256"]
    got = {n: hashlib.sha256(d).hexdigest() for n, d in _members().items()}
    assert got == LOCK["bundle_files"]
    assert LOCK["version"] == "local+ea9e53e5" and LOCK["source_commit"].startswith("ea9e53e5")


def test_the_unpacked_bundle_in_the_package_is_the_pinned_one_when_present():
    d = ROOT / "agentic_os" / "projects_ui"
    if not (d / "index.html").is_file():
        pytest.skip("agentic_os/projects_ui has not been unpacked (editable install makes it)")
    for rel, want in LOCK["bundle_files"].items():
        assert hashlib.sha256((d / rel).read_bytes()).hexdigest() == want, rel


def test_the_pin_chain_is_intact_down_to_the_public_release():
    chain = ["local+9a1788b1", "local+5751e9e5", "local+312a811a", "local+2f24719e", "local+ee958a01", "local+2fdef7f5"]
    node = LOCK
    for want in chain:
        node = node["previous_pin"]
        assert node["version"] == want
        assert hashlib.sha256((ROOT / node["local_path"]).read_bytes()).hexdigest() == node["sha256"], want
    assert node["previous_pin"]["version"] == "v0.2.0"


def test_the_shipped_bundle_passes_every_check():
    m = _members()
    assert problems(_js(m), _css(m)) == []


def test_every_server_refusal_code_has_its_own_fixed_sentence_in_the_bundle():
    js = _js(_members())
    table = sentence_table(js)
    for code in REFUSAL_CODES + OTHER_CODES:
        assert code in table, code
        assert table[code] == GOLDEN[code], code
        assert js.count(code) >= 2, code                              # the table key and the list that decides whether the folder is named
    flat = [s for v in table.values() for s in (v if isinstance(v, tuple) else (v,))]
    assert len(flat) == len(set(flat))                                # no two outcomes say the same thing
    # sentences must say what the page does, never carry a path, a markup character or an interpolated server value
    for code in REFUSAL_CODES:
        assert "${" not in table[code] and "<" not in table[code] and ">" not in table[code], code


def test_the_refusal_codes_cover_the_server_policy_documented_for_the_desktop():
    doc = (ROOT / "docs" / "DESKTOP_SOURCE_REGISTRATION.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"`([a-z_]+)`", doc.split("| Refusal code | Rule |")[1].split("The list is compared")[0]))
    for code in set(REFUSAL_CODES):
        assert code in documented, code
    # the codes the policy module can raise are exactly these 14
    src = (ROOT / "agentic_os" / "source_folder_policy.py").read_text(encoding="utf-8")
    raised = set(re.findall(r'"([a-z_]+)"', src)) & set(REFUSAL_CODES)
    assert raised == set(REFUSAL_CODES)


def test_a_generic_sentence_exists_for_an_unknown_code_and_for_a_failure_without_a_status():
    js = _js(_members())
    for s in FALLBACKS:
        assert s in js, s


def test_the_app_uses_no_browser_dialog_no_raw_html_and_no_inline_outline_removal():
    m = _members()
    js, css = _js(m), _css(m)
    assert not re.search(r"window\.confirm|[^\w.]confirm\(|[^\w.]alert\(|window\.prompt", js)
    assert not re.search(r"dangerouslySetInnerHTML:\s*\{|\{\s*__html\s*:", js)
    assert 'outline:"none"' not in js and "outlineStyle" not in js
    # the two CSS outline removals are on programmatically focused containers (tabindex=-1), never on a control
    assert [x.split("}")[-1] for x in re.findall(r"([^{}]+)\{[^{}]*outline:none", css)] == ["main.viewport", ".drawer"]
    assert "forced-colors:active){:focus-visible{outline:2px solid Highlight" in css


# ------------------------------------------------------------------------------------------------------------------ mutation proof
@pytest.mark.parametrize("code", REFUSAL_CODES + OTHER_CODES)
def test_mutation_a_copy_of_the_bundle_without_one_code_fails_the_checks(code):
    m = _members()
    js, css = _js(m), _css(m)
    mutated = js.replace(f"{code}:", f"x_{code}:", 1)                 # the table key now names another code
    assert mutated != js
    got = problems(mutated, css)
    assert any(code in p for p in got), (code, got)


def test_mutation_a_changed_or_duplicated_sentence_fails():
    m = _members()
    js, css = _js(m), _css(m)
    assert any("home_directory" in p for p in problems(js.replace("Your whole home folder", "Your folder"), css))
    dup = js.replace(GOLDEN["drive_root"], GOLDEN["share_root"], 1)
    assert any("share one sentence" in p for p in problems(dup, css))


def test_mutation_removing_the_generic_fallback_fails():
    m = _members()
    js, css = _js(m), _css(m)
    assert any("generic fallback" in p for p in problems(js.replace("Something went wrong on the runtime, so", "Oops, so"), css))


def test_mutation_a_browser_dialog_raw_html_or_inline_outline_removal_fails():
    m = _members()
    js, css = _js(m), _css(m)
    assert any("browser dialog" in p for p in problems(js + ";window.confirm('sure?')", css))
    assert any("browser dialog" in p for p in problems(js + ";alert(1)", css))
    assert any("raw HTML" in p for p in problems(js + ';h("p",{dangerouslySetInnerHTML:{__html:e.message}})', css))
    assert any("outline" in p for p in problems(js + ';h("input",{style:{outline:"none"}})', css))


def test_mutation_rendering_the_servers_message_fails():
    m = _members()
    js, css = _js(m), _css(m)
    leaked = js.replace("sentence:l,path:", "sentence:(e&&e.message)||l,path:", 1)
    assert leaked != js and any("server's message" in p for p in problems(leaked, css))
    shown = js.replace('s.jsx("span",{children:e.sentence})', 's.jsx("span",{children:e.sentence+e.message})', 1)
    assert shown != js and any("alert component" in p for p in problems(shown, css))


def test_mutation_an_alert_that_cannot_be_dismissed_or_is_not_an_alert_fails():
    m = _members()
    js, css = _js(m), _css(m)
    assert any("alert" in p for p in problems(js.replace('role:"alert",id:n', 'role:"note",id:n', 1), css))
    assert any("dismissed" in p for p in problems(js.replace('onClick:t,children:"Dismiss"', 'onClick:t,children:"Ok"', 1), css))


def test_mutation_a_missing_remove_control_or_confirmation_fails():
    m = _members()
    js, css = _js(m), _css(m)
    assert any("Remove control" in p for p in problems(js.replace('"aria-label":`Remove ${y}`', '', 1), css))
    assert any("Remove control" in p for p in problems(js.replace('N.key==="Escape"&&(N.stopPropagation(),x())', 'N.key==="Never"&&(N.stopPropagation(),x())', 1), css))
    assert any("Remove control" in p for p in problems(js.replace("Yes, remove", "Remove", 1), css))


def test_mutation_a_missing_focus_ring_or_alert_style_fails():
    m = _members()
    js, css = _js(m), _css(m)
    assert any("focus ring" in p for p in problems(js, css.replace(":focus-visible{outline:var(--focus-w) solid", ":focus-visible{outline:none;x:", 1)))
    assert any(".problem{" in p for p in problems(js, css.replace("}.problem{display", "}.prob{display", 1)))
