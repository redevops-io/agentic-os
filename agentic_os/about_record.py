"""The About record (``about.json``, schema ``redevops.about/1``): constants and a strict validator.

Standard library only, no import of the rest of the package, so that three different programs can use the very same code:
``packaging/notices/generate_about.py`` and ``check_about.py`` (build time, loaded by file path) and
``agentic_os/about_ui.py`` (run time, inside the frozen sidecar).

The record is DATA for display. Nothing in the product reads a field of it to make a decision, and no hash in it is a trust
input. The validator therefore checks shape and consistency (exact key sets, types, character classes, sizes, offsets,
slot states), never meaning: it does not judge any licence or legal text.

``validate_record`` returns a list of problems (empty = valid). It never raises on bad input.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Optional

SCHEMA = "redevops.about/1"
MAX_BYTES_WRITE = 1024 * 1024          # the generator refuses to write a larger record
MAX_BYTES_READ = 2 * 1024 * 1024       # the sidecar and the launcher refuse to read a larger one
MAX_SERVED_FILE_BYTES = 4 * 1024 * 1024
MAX_SLOT_TEXT_CHARS = 16384
MAX_COMPONENTS = 2000

# slot id -> decisions it depends on (mirrors ABOUT_SLOTS in packaging/check-distribution-gate.py; a test compares them)
SLOT_DECISIONS: Dict[str, List[str]] = {
    "LEGAL.COPYRIGHT_LINE": ["D-LIC-1"],
    "LEGAL.LICENCE_STATEMENT": ["D-LIC-1", "D-LIC-15"],
    "LEGAL.SOURCE_OFFER": ["D-LIC-12", "pins-03", "pins-04"],
    "LEGAL.NO_WARRANTY": ["D-LIC-10"],
    "LEGAL.SECTION13_NOTICE": ["D-LIC-5"],
}
OMITTABLE_SLOTS = ("LEGAL.SECTION13_NOTICE",)
SLOT_STATES = ("pending", "approved-verified", "unverified-draft", "not-applicable")
SLOT_TAGS = {"pending": "Pending a decision", "approved-verified": "Approved text, signature verified",
             "unverified-draft": "Unverified draft, not an approved text", "not-applicable": ""}
DRAFT_PREFIX = ("Unverified draft, not an approved text. This is a private candidate; nothing here is a legal statement or "
                "clearance.\n\n")
BUILD_MODES = ("private-candidate", "distribution")
SOURCE_ACCESS_STATES = ("not-published", "offer-recorded")
PLACEHOLDER_PREFIX = "Legal text pending decision "
LICENCE_ROLES = ("licence-file", "addendum-file", "statement-file", "overview-file", "gaps-file")
COMPONENT_STATUS = ("text-found", "text-not-found", "not-third-party")

HEX64 = re.compile(r"^[0-9a-f]{64}$")
HEX40 = re.compile(r"^[0-9a-f]{40}$")
_VERSION = re.compile(r"^[0-9]+(\.[0-9]+){0,3}([-+][0-9A-Za-z.+-]{1,40})?$")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_PINVERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,39}$")
UI_PIN_VERIFIED = ("wheel", "directory")
_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_BIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_TOKEN = re.compile(r"^[a-z0-9][a-z0-9.-]{0,31}$")
_BUNDLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,99}$")
_TIME = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_DESCRIBE = re.compile(r"^[A-Za-z0-9._+-]{1,100}$")
_RELPATH = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+){0,5}$")
_DECISION = re.compile(r"^(D-LIC-[0-9]{1,2}|pins-[0-9]{2})$")
# A host path, user name, scratch or worktree name must never get into the record (the RC2 build manifest has some).
_PATHLIKE = re.compile(r"(/Users/|/private/|/home/|/var/folders|/tmp/|\\\\|[A-Za-z]:\\|(?<![A-Za-z0-9])wt-|file://|\.\./)")


def _printable_ascii(s: str) -> bool:
    return all(32 <= ord(c) < 127 for c in s)


class _Ctx:
    def __init__(self) -> None:
        self.errs: List[str] = []

    def bad(self, where: str, msg: str) -> None:
        if len(self.errs) < 60:
            self.errs.append(f"{where}: {msg}")

    def obj(self, where: str, v: Any, keys: tuple, optional: tuple = ()) -> bool:
        if not isinstance(v, dict):
            self.bad(where, "must be an object")
            return False
        extra = sorted(set(v) - set(keys) - set(optional))
        missing = sorted(set(keys) - set(v))
        if extra:
            self.bad(where, f"unexpected field(s) {extra}")
        if missing:
            self.bad(where, f"missing field(s) {missing}")
        return not extra and not missing

    def text(self, where: str, v: Any, rx: Optional["re.Pattern[str]"] = None, maxlen: int = 200, allow_empty: bool = False) -> bool:
        if not isinstance(v, str):
            self.bad(where, "must be a string")
            return False
        if not v and not allow_empty:
            self.bad(where, "must not be empty")
            return False
        if len(v) > maxlen:
            self.bad(where, f"longer than {maxlen} characters")
            return False
        if not _printable_ascii(v):
            self.bad(where, "must be printable ASCII")
            return False
        if rx is not None and not rx.match(v):
            self.bad(where, "has an unexpected form")
            return False
        if _PATHLIKE.search(v):
            self.bad(where, "looks like a host path or scratch name")
            return False
        return True

    def num(self, where: str, v: Any, lo: int = 0, hi: int = 1 << 40) -> bool:
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            self.bad(where, f"must be an integer in [{lo}, {hi}]")
            return False
        return True

    def hexs(self, where: str, v: Any, rx: "re.Pattern[str]" = HEX64) -> bool:
        if not isinstance(v, str) or not rx.match(v):
            self.bad(where, "must be a lowercase hex digest")
            return False
        return True

    def lst(self, where: str, v: Any, maxlen: int) -> bool:
        if not isinstance(v, list):
            self.bad(where, "must be a list")
            return False
        if len(v) > maxlen:
            self.bad(where, f"more than {maxlen} entries")
            return False
        return True


def canonical_bytes(doc: Dict[str, Any]) -> bytes:
    """The one serialisation: sorted keys, 1-space indent, ASCII only (non-ASCII is escaped), final LF."""
    return (json.dumps(doc, sort_keys=True, indent=1, ensure_ascii=True) + "\n").encode("ascii")


def validate_record(doc: Any) -> List[str]:
    c = _Ctx()
    if not isinstance(doc, dict):
        return ["record: must be a JSON object"]
    if not c.obj("record", doc, ("schema", "product", "build", "licence_files", "notices", "legal", "source_access", "ui")):
        return c.errs
    if doc["schema"] != SCHEMA:
        c.bad("schema", f"must be {SCHEMA!r}")
    _product(c, doc["product"])
    _build(c, doc["build"])
    _licence_files(c, doc["licence_files"])
    _notices(c, doc["notices"])
    _legal(c, doc["legal"], doc["build"], doc["source_access"])
    if c.obj("ui", doc["ui"], ("entry", "locales")):
        if doc["ui"]["entry"] is not True:
            c.bad("ui.entry", "must be true")
        if doc["ui"]["locales"] != ["en"]:
            c.bad("ui.locales", 'must be ["en"]')
    return c.errs


def _product(c: _Ctx, p: Any) -> None:
    if not c.obj("product", p, ("name", "version", "release_line", "bundle_identifier", "launcher", "sidecar")):
        return
    c.text("product.name", p["name"], maxlen=80)
    c.text("product.version", p["version"], _VERSION, 40)
    c.text("product.release_line", p["release_line"], _TOKEN, 32)
    c.text("product.bundle_identifier", p["bundle_identifier"], _BUNDLE, 100)
    if c.obj("product.launcher", p["launcher"], ("binary", "version")):
        c.text("product.launcher.binary", p["launcher"]["binary"], _BIN, 64)
        c.text("product.launcher.version", p["launcher"]["version"], _VERSION, 40)
    if c.obj("product.sidecar", p["sidecar"], ("binary", "python_package", "python_package_version")):
        c.text("product.sidecar.binary", p["sidecar"]["binary"], _BIN, 64)
        c.text("product.sidecar.python_package", p["sidecar"]["python_package"], _BIN, 64)
        c.text("product.sidecar.python_package_version", p["sidecar"]["python_package_version"], _VERSION, 40)


def _build(c: _Ctx, b: Any) -> None:
    if not c.obj("build", b, ("mode", "platform", "source", "ui_bundle", "inputs")):
        return
    if b["mode"] not in BUILD_MODES:
        c.bad("build.mode", f"must be one of {BUILD_MODES}")
    c.text("build.platform", b["platform"], _TOKEN, 32)
    s = b["source"]
    if c.obj("build.source", s, ("commit", "tree", "dirty_files", "commit_time_utc", "describe")):
        c.hexs("build.source.commit", s["commit"], HEX40)
        c.hexs("build.source.tree", s["tree"], HEX40)
        c.num("build.source.dirty_files", s["dirty_files"], 0, 1_000_000)
        c.text("build.source.commit_time_utc", s["commit_time_utc"], _TIME, 20)
        c.text("build.source.describe", s["describe"], _DESCRIBE, 100)
    u = b["ui_bundle"]
    if c.obj("build.ui_bundle", u, ("id", "files"), ("pin",)):
        c.text("build.ui_bundle.id", u["id"], _FILE, 64)
        if "pin" in u:
            _ui_pin(c, u["pin"])
        if c.lst("build.ui_bundle.files", u["files"], 64):
            for k, f in enumerate(u["files"]):
                w = f"build.ui_bundle.files[{k}]"
                if c.obj(w, f, ("path", "bytes", "sha256")):
                    c.text(w + ".path", f["path"], _RELPATH, 120)
                    c.num(w + ".bytes", f["bytes"], 0, 1 << 32)
                    c.hexs(w + ".sha256", f["sha256"])
    i = b["inputs"]
    if c.obj("build.inputs", i, ("sbom_sha256", "gate_file_sha256")):
        c.hexs("build.inputs.sbom_sha256", i["sbom_sha256"])
        c.hexs("build.inputs.gate_file_sha256", i["gate_file_sha256"])


def _ui_pin(c: _Ctx, p: Any) -> None:
    """The pinned Projects UI (packaging/projects_ui.lock.json) the bundle was verified against at build time. Optional: older records
    and unpinned builds carry no pin."""
    w = "build.ui_bundle.pin"
    if not c.obj(w, p, ("version", "artifact_sha256", "source_commit", "source_commit_published", "verified_against")):
        return
    c.text(w + ".version", p["version"], _PINVERSION, 40)
    c.hexs(w + ".artifact_sha256", p["artifact_sha256"])
    c.hexs(w + ".source_commit", p["source_commit"], HEX40)
    if not isinstance(p["source_commit_published"], bool):
        c.bad(w + ".source_commit_published", "must be true or false")
    if p["verified_against"] not in UI_PIN_VERIFIED:
        c.bad(w + ".verified_against", f"must be one of {UI_PIN_VERIFIED}")


def _licence_files(c: _Ctx, files: Any) -> None:
    if not c.lst("licence_files", files, 16):
        return
    seen = set()
    for k, f in enumerate(files):
        w = f"licence_files[{k}]"
        if not c.obj(w, f, ("id", "file", "label", "bytes", "sha256", "role")):
            continue
        c.text(w + ".id", f["id"], _SLUG, 40)
        c.text(w + ".file", f["file"], _FILE, 64)
        c.text(w + ".label", f["label"], maxlen=120)
        c.num(w + ".bytes", f["bytes"], 0, MAX_SERVED_FILE_BYTES)
        c.hexs(w + ".sha256", f["sha256"])
        if f["role"] not in LICENCE_ROLES:
            c.bad(w + ".role", f"must be one of {LICENCE_ROLES}")
        if f["id"] in seen:
            c.bad(w + ".id", "duplicate")
        seen.add(f["id"])
        if f["id"] == "notices":
            c.bad(w + ".id", "'notices' is reserved for the notices file")


def _notices(c: _Ctx, n: Any) -> None:
    if not c.obj("notices", n, ("file", "bytes", "sha256", "component_count", "components", "reference_notices", "missing")):
        return
    c.text("notices.file", n["file"], _FILE, 64)
    ok_size = c.num("notices.bytes", n["bytes"], 0, MAX_SERVED_FILE_BYTES)
    c.hexs("notices.sha256", n["sha256"])
    c.num("notices.component_count", n["component_count"], 0, MAX_COMPONENTS)
    size = n["bytes"] if ok_size else 0
    last_end = 0
    if c.lst("notices.components", n["components"], MAX_COMPONENTS):
        if n["component_count"] != len(n["components"]):
            c.bad("notices.component_count", "differs from the number of components")
        for k, comp in enumerate(n["components"]):
            w = f"notices.components[{k}]"
            if not c.obj(w, comp, ("n", "name", "version", "declared", "type", "status", "offset", "length", "files")):
                continue
            if c.num(w + ".n", comp["n"], 1, MAX_COMPONENTS) and comp["n"] != k + 1:
                c.bad(w + ".n", "must be the 1-based position")
            c.text(w + ".name", comp["name"], maxlen=120)
            c.text(w + ".version", comp["version"], maxlen=80, allow_empty=True)
            c.text(w + ".declared", comp["declared"], maxlen=300)
            c.text(w + ".type", comp["type"], _TOKEN, 32)
            if comp["status"] not in COMPONENT_STATUS:
                c.bad(w + ".status", f"must be one of {COMPONENT_STATUS}")
            if c.num(w + ".offset", comp["offset"], 0, size) and c.num(w + ".length", comp["length"], 1, size):
                if comp["offset"] < last_end or comp["offset"] + comp["length"] > size:
                    c.bad(w, "offset/length outside the notices file or overlapping the previous section")
                last_end = comp["offset"] + comp["length"]
            if c.lst(w + ".files", comp["files"], 40):
                for j, f in enumerate(comp["files"]):
                    ww = f"{w}.files[{j}]"
                    if c.obj(ww, f, ("label", "bytes", "sha256")):
                        c.text(ww + ".label", f["label"], maxlen=200)
                        c.num(ww + ".bytes", f["bytes"], 0, 1 << 28)
                        c.hexs(ww + ".sha256", f["sha256"])
    if c.lst("notices.reference_notices", n["reference_notices"], 100):
        for k, r in enumerate(n["reference_notices"]):
            w = f"notices.reference_notices[{k}]"
            if c.obj(w, r, ("id", "title", "declared", "status", "offset", "length")):
                c.text(w + ".id", r["id"], _SLUG, 40)
                c.text(w + ".title", r["title"], maxlen=200)
                c.text(w + ".declared", r["declared"], maxlen=300, allow_empty=True)
                if r["status"] not in COMPONENT_STATUS:
                    c.bad(w + ".status", f"must be one of {COMPONENT_STATUS}")
                if c.num(w + ".offset", r["offset"], 0, size) and c.num(w + ".length", r["length"], 1, size):
                    if r["offset"] < last_end or r["offset"] + r["length"] > size:
                        c.bad(w, "offset/length outside the notices file or overlapping the previous section")
                    last_end = r["offset"] + r["length"]
    if c.lst("notices.missing", n["missing"], MAX_COMPONENTS):
        for k, m in enumerate(n["missing"]):
            w = f"notices.missing[{k}]"
            if c.obj(w, m, ("name", "version")):
                c.text(w + ".name", m["name"], maxlen=120)
                c.text(w + ".version", m["version"], maxlen=80, allow_empty=True)


def _legal(c: _Ctx, legal: Any, build: Any, source_access: Any) -> None:
    if not c.obj("legal", legal, ("slots",)):
        return
    slots = legal["slots"]
    if not c.obj("legal.slots", slots, tuple(SLOT_DECISIONS)):
        return
    mode = build.get("mode") if isinstance(build, dict) else None
    for sid, ids in SLOT_DECISIONS.items():
        s = slots[sid]
        w = f"legal.slots.{sid}"
        if not isinstance(s, dict):
            c.bad(w, "must be an object")
            continue
        has_sha = s.get("state") in ("approved-verified", "unverified-draft")
        if not c.obj(w, s, ("state", "tag", "decisions", "pending", "text") + (("text_sha256",) if has_sha else ())):
            continue
        st = s["state"]
        if st not in SLOT_STATES:
            c.bad(w + ".state", f"must be one of {SLOT_STATES}")
            continue
        if s["tag"] != SLOT_TAGS[st]:
            c.bad(w + ".tag", f"must be {SLOT_TAGS[st]!r}")
        if s["decisions"] != ids:
            c.bad(w + ".decisions", f"must be {ids}")
        if not isinstance(s["pending"], list) or any(p not in ids for p in s["pending"]):
            c.bad(w + ".pending", "must list decisions of this slot")
            continue
        text = s["text"]
        if not isinstance(text, str) or len(text) > MAX_SLOT_TEXT_CHARS + len(DRAFT_PREFIX):
            c.bad(w + ".text", f"must be a string of at most {MAX_SLOT_TEXT_CHARS} characters (plus the draft label)")
            continue
        if st == "pending":
            if not s["pending"]:
                c.bad(w, "pending slot without a pending decision")
            if not text.startswith(PLACEHOLDER_PREFIX) or "private candidate" not in text or "pending" not in text:
                c.bad(w + ".text", "a placeholder must say that the text is pending a decision and that this is a private candidate")
            if mode == "distribution":
                c.bad(w, "a distribution record must not carry a pending slot")
        elif st == "approved-verified":
            if s["pending"]:
                c.bad(w, "approved slot with a pending decision")
            if not text.strip():
                c.bad(w + ".text", "empty")
            if hashlib.sha256(text.encode("utf-8")).hexdigest() != s.get("text_sha256"):
                c.bad(w + ".text_sha256", "does not match the text")
        elif st == "unverified-draft":
            if s["pending"]:
                c.bad(w, "a draft has no pending decision")
            if not text.startswith(DRAFT_PREFIX):
                c.bad(w + ".text", "an unverified draft must start with its label (it is never shown as ordinary legal text)")
            elif hashlib.sha256(text[len(DRAFT_PREFIX):].encode("utf-8")).hexdigest() != s.get("text_sha256"):
                c.bad(w + ".text_sha256", "does not match the draft text")
            if mode == "distribution":
                c.bad(w, "a distribution record must not carry an unverified draft")
        else:                                           # not-applicable
            if sid not in OMITTABLE_SLOTS:
                c.bad(w + ".state", "this slot cannot be omitted")
            if s["pending"] or text:
                c.bad(w, "a not-applicable slot has no text and no pending decision")
    if c.obj("source_access", source_access, ("state",)):
        st = source_access["state"]
        if st not in SOURCE_ACCESS_STATES:
            c.bad("source_access.state", f"must be one of {SOURCE_ACCESS_STATES}")
        else:
            offer = slots["LEGAL.SOURCE_OFFER"]
            if isinstance(offer, dict) and (st == "offer-recorded") != (offer.get("state") == "approved-verified"):
                c.bad("source_access.state", "must be offer-recorded exactly when LEGAL.SOURCE_OFFER is approved-verified")


def parse_record(raw: bytes) -> "tuple[Optional[Dict[str, Any]], List[str]]":
    """Bytes of about.json -> (document, problems). Size-capped; rejects duplicate keys, NaN, non-ASCII bytes."""
    if len(raw) > MAX_BYTES_READ:
        return None, [f"record larger than {MAX_BYTES_READ} bytes"]
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        return None, ["record is not ASCII"]

    def no_dupes(pairs):
        d = {}
        for k, v in pairs:
            if k in d:
                raise ValueError(f"duplicate key {k!r}")
            d[k] = v
        return d

    def no_const(name):
        raise ValueError(f"constant {name} not allowed")

    try:
        doc = json.loads(text, object_pairs_hook=no_dupes, parse_constant=no_const)
    except (ValueError, RecursionError) as e:
        return None, [f"record is not valid JSON: {str(e)[:120]}"]
    errs = validate_record(doc)
    return (doc if not errs else None), errs


# Words the product's own About strings must not contain (a placeholder may only use them inside its explicit negation
# sentence, which the current placeholder does not need). Third-party notice texts and an owner-approved slot text are not product
# strings and are not linted.
FORBIDDEN_PHRASES = ("open source", "cleared", "compliant", "approved by counsel", "legally")


def forbidden_in(text: str) -> List[str]:
    low = text.lower()
    return [p for p in FORBIDDEN_PHRASES if p in low]


def record_strings(doc: Any) -> List[str]:
    """Every string of the record except the text of an approved-verified slot or unverified draft (the owner's wording)."""
    out: List[str] = []

    def walk(v: Any, skip_text: bool = False) -> None:
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, dict):
            filled = v.get("state") in ("approved-verified", "unverified-draft") and "text_sha256" in v
            for k, x in v.items():
                out.append(k)
                if filled and k == "text":
                    continue
                walk(x)
    walk(doc)
    return out
