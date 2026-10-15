"""Typed patch operations: the ONLY edit vocabulary. No free-form code ever reaches an editor.

A patch is a list of operations validated here, simulated locally to produce a preview diff and the expected result,
and finally executed by a backend from a FIXED script template (``builder_script``) that receives the operations as
JSON data -- never as interpolated source. The template text is constant and reviewable; its SHA-256 is recorded in
every proposal.

Operations
    replace_text      {search, replace, match_case?}      docx  (tracked deletion+insertion when track_changes)
    append_paragraph  {text}                              docx  (tracked insertion when track_changes)
    set_cell          {cell, value, sheet?}               xlsx  (no revision tracking exists for cells)
"""
from __future__ import annotations

import difflib
import hashlib
import json
from typing import Any

from . import ooxml
from .errors import InvalidInput

MAX_OPS = 200
MAX_SEARCH, MAX_TEXT, MAX_CELL_TEXT = 500, 20000, 5000
OPS = {
    "replace_text": {"docx"},
    "append_paragraph": {"docx"},
    "set_cell": {"xlsx"},
}


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _str(op: dict, key: str, lo: int, hi: int) -> str:
    v = op.get(key)
    if not isinstance(v, str) or not (lo <= len(v) <= hi):
        raise InvalidInput(f"{op.get('type')}.{key} must be a string of {lo}..{hi} characters")
    if any(ord(c) < 32 and c not in "\t" for c in v):
        raise InvalidInput(f"{op.get('type')}.{key} contains control characters")
    return v


def normalize_ops(ops: Any, fmt: str) -> "list[dict]":
    """Validate and canonicalise. Unknown types / keys / formats are refused (fail closed)."""
    if not isinstance(ops, list) or not ops:
        raise InvalidInput("ops must be a non-empty list")
    if len(ops) > MAX_OPS:
        raise InvalidInput(f"at most {MAX_OPS} operations per patch")
    out: list[dict] = []
    for op in ops:
        if not isinstance(op, dict) or op.get("type") not in OPS:
            raise InvalidInput(f"unknown operation {op.get('type') if isinstance(op, dict) else op!r}")
        t = op["type"]
        if fmt not in OPS[t]:
            raise InvalidInput(f"operation {t} does not apply to {fmt} documents")
        if t == "replace_text":
            extra = set(op) - {"type", "search", "replace", "match_case"}
            if extra:
                raise InvalidInput(f"replace_text: unknown key(s) {sorted(extra)}")
            mc = op.get("match_case", True)
            if not isinstance(mc, bool):
                raise InvalidInput("replace_text.match_case must be a boolean")
            out.append({"type": t, "search": _str(op, "search", 1, MAX_SEARCH),
                        "replace": _str(op, "replace", 0, MAX_TEXT), "match_case": mc})
        elif t == "append_paragraph":
            extra = set(op) - {"type", "text"}
            if extra:
                raise InvalidInput(f"append_paragraph: unknown key(s) {sorted(extra)}")
            out.append({"type": t, "text": _str(op, "text", 1, MAX_TEXT)})
        else:
            extra = set(op) - {"type", "cell", "value", "sheet"}
            if extra:
                raise InvalidInput(f"set_cell: unknown key(s) {sorted(extra)}")
            cell = op.get("cell")
            if not ooxml.valid_cell_ref(cell):
                raise InvalidInput("set_cell.cell must be an A1-style reference")
            sheet = op.get("sheet", 1)
            if isinstance(sheet, bool) or not isinstance(sheet, int) or not (1 <= sheet <= 50):
                raise InvalidInput("set_cell.sheet must be a worksheet number 1..50")
            val = op.get("value")
            if isinstance(val, bool) or not isinstance(val, (str, int, float)):
                raise InvalidInput("set_cell.value must be a string or number")
            if isinstance(val, str):
                if len(val) > MAX_CELL_TEXT or any(ord(c) < 32 for c in val):
                    raise InvalidInput("set_cell.value is too long or has control characters")
                if val.lstrip()[:1] in ("=", "+", "-", "@"):
                    raise InvalidInput("set_cell.value must not start with a formula character (= + - @)")
            out.append({"type": t, "cell": cell, "value": val, "sheet": sheet})
    return out


def patch_digest(ops: "list[dict]", track_changes: bool) -> str:
    return hashlib.sha256(canonical({"ops": ops, "track_changes": bool(track_changes)})).hexdigest()


def simulate_docx(paragraphs: "list[str]", ops: "list[dict]") -> "tuple[list[str], list[dict]]":
    """Apply the ops to a paragraph-text list. Returns (new paragraphs, per-op findings). A replace_text whose
    search string occurs in no paragraph is a validation error (the patch would be a silent no-op)."""
    cur = list(paragraphs)
    findings: list[dict] = []
    for i, op in enumerate(ops):
        if op["type"] == "replace_text":
            n = 0
            for j, p in enumerate(cur):
                hay, needle = (p, op["search"]) if op["match_case"] else (p.lower(), op["search"].lower())
                c = hay.count(needle)
                if c:
                    n += c
                    cur[j] = _replace(p, op["search"], op["replace"], op["match_case"])
            findings.append({"op": i, "type": op["type"], "occurrences": n,
                             "ok": n > 0, **({} if n else {"problem": "search text not found"})})
        elif op["type"] == "append_paragraph":
            cur.append(op["text"])
            findings.append({"op": i, "type": op["type"], "ok": True})
    return cur, findings


def _replace(text: str, search: str, repl: str, match_case: bool) -> str:
    if match_case:
        return text.replace(search, repl)
    out, low, s, i = [], text.lower(), search.lower(), 0
    while True:
        k = low.find(s, i)
        if k < 0:
            out.append(text[i:])
            return "".join(out)
        out.append(text[i:k])
        out.append(repl)
        i = k + len(search)


def unified_preview(before: "list[str]", after: "list[str]", name: str = "document") -> str:
    return "\n".join(difflib.unified_diff(before, after, f"{name} (base)", f"{name} (proposed)", lineterm="", n=1))


# ── the fixed Document Builder script templates ───────────────────────────────────────────────────────────────
# Operations arrive through the documented `argument` request parameter (JSON, exposed to the script as the global
# `Argument`). Nothing from a document or a caller is ever concatenated into this source.
_DOCX_TEMPLATE = """\
builderJS.OpenFile(Argument["src"]);
var d = Api.GetDocument();
if (Argument["track"]) { d.SetTrackRevisions(true); }
var ops = Argument["ops"];
for (var i = 0; i < ops.length; i++) {
  var op = ops[i];
  if (op["type"] === "replace_text") {
    d.SearchAndReplace({searchString: op["search"], replaceString: op["replace"], matchCase: !!op["match_case"]});
  } else if (op["type"] === "append_paragraph") {
    var p = Api.CreateParagraph();
    p.AddText(op["text"]);
    d.Push(p);
  }
}
builderJS.SaveFile("docx", "result.docx");
builderJS.CloseFile();
"""

_XLSX_TEMPLATE = """\
builderJS.OpenFile(Argument["src"]);
var ops = Argument["ops"];
for (var i = 0; i < ops.length; i++) {
  var op = ops[i];
  if (op["type"] === "set_cell") {
    var ws = Api.GetSheet(op["sheet"] - 1);
    ws.GetRange(op["cell"]).SetValue(op["value"]);
  }
}
builderJS.SaveFile("xlsx", "result.xlsx");
builderJS.CloseFile();
"""

TEMPLATES = {"docx": _DOCX_TEMPLATE, "xlsx": _XLSX_TEMPLATE}


def builder_script(fmt: str) -> bytes:
    if fmt not in TEMPLATES:
        raise InvalidInput(f"no edit template for {fmt}")
    return TEMPLATES[fmt].encode("utf-8")


def template_digest(fmt: str) -> str:
    return hashlib.sha256(builder_script(fmt)).hexdigest()
