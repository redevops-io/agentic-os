"""Untrusted-content hardening. Public website text and model output are DATA, never instructions.

* :func:`extract_visible_text` reduces HTML to visible text with the stdlib parser: scripts, styles, comments,
  templates, SVG, iframes, ``hidden`` / ``aria-hidden`` / ``display:none`` / visually-hidden elements and ALL
  attributes are dropped (nothing from an attribute is carried through), zero-width and bidi control characters
  are removed, text is NFC-normalised.
* :func:`detect_injection` finds instruction-like strings (ignore-previous, role override, tool-call syntax,
  reveal-prompt, exfiltration, markdown/HTML injection, chat-template tokens, base64 blobs); :func:`neutralise`
  replaces them with an inert marker and returns what it did, so counts can go in the manifest
  (``untrusted_content_flags``).
* :func:`wrap_untrusted` puts the text into a delimited, escaped block whose end marker contains a hash of the
  content (an attacker cannot embed a valid end marker: it would have to contain its own hash).
* :func:`quarantine_reason` inspects a RESPONSE: a URL, tool-call syntax, chat-template tokens, a base64 blob or an
  instruction addressed to the operator quarantines the response (it is excluded from scoring and counted).

None of this performs I/O. Detection is heuristic (it reports and neutralises; it is not a guarantee), which is
why the surrounding design also gives the model no tools, no browsing, no files and no network, and only ever
parses its output as text.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple

# ── characters ─────────────────────────────────────────────────────────────────────────────────────────────
_INVISIBLE = re.compile("[­​-‏‪-‮⁠-⁤⁦-⁩﻿᠎؜]")
_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def strip_invisible(text: str) -> Tuple[str, int]:
    """Remove zero-width, bidi and other control characters; returns (clean, number removed)."""
    n = len(_INVISIBLE.findall(text)) + len(_CONTROL.findall(text))
    return _CONTROL.sub("", _INVISIBLE.sub("", text)), n


# ── HTML -> visible text ───────────────────────────────────────────────────────────────────────────────────
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_SKIP = {"script", "style", "noscript", "template", "svg", "iframe", "object", "embed", "canvas", "head", "math"}
_BLOCK = {"p", "div", "section", "article", "header", "footer", "main", "nav", "aside", "ul", "ol", "li", "br", "tr",
          "table", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "form", "figure", "figcaption", "details",
          "summary", "dl", "dt", "dd", "hr", "pre"}
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0|opacity\s*:\s*0(?![.\d])"
                           r"|clip\s*:\s*rect\(0|text-indent\s*:\s*-\d{3,}", re.I)
_HIDDEN_CLASS = re.compile(r"(^|[\s_-])(sr-only|visually-hidden|screen-reader-text|hidden|d-none|invisible)($|[\s_-])", re.I)


@dataclass
class ExtractedPage:
    title: str = ""
    description: str = ""
    headings: List[str] = field(default_factory=list)
    ctas: List[str] = field(default_factory=list)
    text: str = ""
    removed: Dict[str, int] = field(default_factory=dict)


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: List[str] = []
        self.title_parts: List[str] = []
        self.description = ""
        self.headings: List[str] = []
        self.ctas: List[str] = []
        self.removed: Dict[str, int] = {}
        self._skip_depth = 0
        self._stack: List[Tuple[str, bool]] = []     # (tag, started_skip)
        self._in_title = False
        self._heading: Optional[List[str]] = None
        self._cta: Optional[List[str]] = None
        self._head_depth = 0

    def _bump(self, k: str) -> None:
        self.removed[k] = self.removed.get(k, 0) + 1

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta" and a.get("name", "").lower() == "description" and not self.description:
            self.description = a.get("content", "")
        if tag in _VOID:
            return
        hidden = (tag in _SKIP and tag != "head") or "hidden" in a or a.get("aria-hidden", "").lower() == "true" \
            or bool(_HIDDEN_STYLE.search(a.get("style", ""))) or bool(_HIDDEN_CLASS.search(a.get("class", "")))
        if tag == "head":
            hidden = True
        if tag == "title":
            self._in_title = True
        starts = hidden and self._skip_depth == 0
        if hidden:
            self._skip_depth += 1
            if starts:
                self._bump(f"hidden:{tag}" if tag not in _SKIP else f"dropped:{tag}")
        self._stack.append((tag, hidden))
        if self._skip_depth:
            return
        if tag in _BLOCK:
            self.out.append("\n")
        if tag in ("h1", "h2", "h3"):
            self._heading = []
        if tag == "button" or (tag == "a" and re.search(r"btn|button|cta", a.get("class", ""), re.I)):
            self._cta = []

    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        # pop to the matching open tag (tolerate unbalanced markup)
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                for t, h in self._stack[i:]:
                    if h:
                        self._skip_depth = max(0, self._skip_depth - 1)
                del self._stack[i:]
                break
        else:
            return
        if tag == "title":
            self._in_title = False
        if self._skip_depth:
            return
        if tag in ("h1", "h2", "h3") and self._heading is not None:
            h = " ".join("".join(self._heading).split())
            if h:
                self.headings.append(h)
            self._heading = None
        if tag in ("button", "a") and self._cta is not None:
            c = " ".join("".join(self._cta).split())
            if c and len(c) <= 80 and c not in self.ctas:
                self.ctas.append(c)
            self._cta = None
        if tag in _BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title_parts.append(data)
        if self._skip_depth:
            return
        self.out.append(data)
        if self._heading is not None:
            self._heading.append(data)
        if self._cta is not None:
            self._cta.append(data)

    def handle_comment(self, data):                        # comments never reach the text
        self._bump("comment")


def extract_visible_text(html: str) -> ExtractedPage:
    p = _Extractor()
    p.feed(html)
    p.close()
    raw, n_inv = strip_invisible("".join(p.out))
    lines = [" ".join(ln.split()) for ln in raw.splitlines()]
    text = unicodedata.normalize("NFC", "\n".join(ln for ln in lines if ln))
    title, _ = strip_invisible(" ".join("".join(p.title_parts).split()))
    desc, _ = strip_invisible(" ".join(p.description.split()))
    removed = dict(p.removed)
    if n_inv:
        removed["invisible_chars"] = n_inv
    return ExtractedPage(title=title, description=desc, headings=[strip_invisible(h)[0] for h in p.headings],
                         ctas=[strip_invisible(c)[0] for c in p.ctas], text=text, removed=removed)


# ── injection detection / neutralisation ───────────────────────────────────────────────────────────────────
_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = tuple((k, re.compile(v, re.I | re.S)) for k, v in (
    ("ignore_previous", r"\b(?:ignore|disregard|forget|override)\b[^.\n]{0,40}\b(?:previous|prior|above|earlier|all|any|your|the)\b"
                        r"[^.\n]{0,30}\b(?:instructions?|prompts?|rules?|persona|context|guidelines?)\b"),
    ("role_override", r"\b(?:you are now|from now on,? you|act as (?:an?|the)|pretend (?:to be|you)|new instructions?|"
                      r"developer mode|jailbreak|do anything now)\b"),
    ("system_prompt", r"\b(?:reveal|print|show|repeat|output|leak|disclose)\b[^.\n]{0,40}\b(?:system|hidden|initial|developer)\s+(?:prompt|message|instructions?)\b|"
                      r"\bsystem prompt\b"),
    ("tool_call", r"(?:\bcall (?:the )?(?:tool|function)\b|\buse (?:the )?(?:tool|function)\b|<tool_call>|</tool_call>|"
                  r"\"tool_calls?\"|\bfunction_call\b|\"name\"\s*:\s*\"[^\"]+\"\s*,\s*\"arguments\")"),
    ("exfiltration", r"\b(?:send|post|upload|forward|email|exfiltrate|transmit)\b[^.\n]{0,60}\b(?:to|at)\b[^.\n]{0,20}"
                     r"(?:https?://|www\.|[\w.-]+@)\S*"),
    ("score_steering", r"\b(?:rate|score|answer|respond|reply|output|give|assign|set|mark)\b[^.\n]{0,60}\b(?:5\s*/\s*5|[1-5] out of [1-5]|five stars?|"
                       r"(?:as|with) (?:a )?(?:5|five)|(?:very|extremely|highly) (?:likely|positive)|definitely (?:buy|purchase))\b"),
    ("format_override", r"\b(?:respond|reply|answer|output)\s+(?:only |exactly )?(?:with|in|as)\s+(?:json|xml|html|markdown|a single|the word|yes|no)\b"),
    ("markdown_link", r"!?\[[^\]]{0,200}\]\((?:https?:)?//[^)]*\)"),
    ("html_injection", r"<\s*(?:script|iframe|img|object|embed|svg|style|link|meta)\b|\bon(?:error|load|click)\s*="),
    ("template_token", r"<\|[a-z_]+\|>|\[/?INST\]|<<\s*SYS\s*>>|</?think>|<\|im_(?:start|end)\|>|^\s*(?:system|assistant)\s*:"),
    ("base64_blob", r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{60,}={0,2}(?![A-Za-z0-9+/])"),
    ("delimiter_spoof", r"PAGE_TEXT_(?:BEGIN|END)|<<<|>>>"),
))
_URL = re.compile(r"(?:https?://|www\.)[^\s)>\]\"']+", re.I)


@dataclass(frozen=True)
class Flag:
    kind: str
    count: int
    example: str


def detect_injection(text: str) -> List[Flag]:
    """Instruction-like strings found in ``text`` (heuristic). Detection runs on an NFKC copy so fullwidth and
    compatibility-form obfuscation does not hide a pattern."""
    probe = unicodedata.normalize("NFKC", strip_invisible(text)[0])
    flags: List[Flag] = []
    for kind, pat in _PATTERNS:
        found = list(pat.finditer(probe))
        if found:
            flags.append(Flag(kind, len(found), found[0].group(0)[:80]))
    return flags


def neutralise(text: str) -> Tuple[str, List[Flag]]:
    """Return ``(inert_text, flags)``: invisible characters removed, instruction-like spans and URLs replaced by
    fixed markers. The result is what may be placed in a prompt (inside :func:`wrap_untrusted`)."""
    clean, _ = strip_invisible(text)
    clean = unicodedata.normalize("NFKC", clean)
    flags = detect_injection(clean)
    for kind, pat in _PATTERNS:
        clean = pat.sub("[removed: instruction-like text]", clean)
    clean = _URL.sub("[link]", clean)
    clean = clean.replace("<", "(").replace(">", ")")
    return clean, flags


def wrap_untrusted(text: str, label: str = "page text") -> str:
    """Delimited block. The end marker embeds the sha256 prefix of the content, so content cannot close the block."""
    body = text.replace("PAGE_TEXT", "page text").replace("<<<", "(").replace(">>>", ")")
    tag = hashlib.sha256(body.encode("utf-8")).hexdigest()[:10]
    return f"<<<PAGE_TEXT_BEGIN {tag} ({label}; untrusted data)>>>\n{body}\n<<<PAGE_TEXT_END {tag}>>>"


# ── responses ──────────────────────────────────────────────────────────────────────────────────────────────
_RESP_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("url", re.compile(r"(?:https?://|www\.)\S+|\b[a-z0-9-]{2,}\.(?:com|io|app|net|org|ai|dev|co|xyz|ru|cn)\b/?\S*", re.I)),
    ("tool_call_syntax", re.compile(r"<tool_call>|</tool_call>|\"tool_calls?\"|\bfunction_call\b|\"arguments\"\s*:|"
                                    r"```\s*(?:json|python|bash|sh)\b|\bcall(?:ing)? (?:the )?(?:tool|function)\b", re.I)),
    ("template_token", re.compile(r"<\|[a-z_]+\|>|\[/?INST\]|<<\s*SYS\s*>>|<think>", re.I)),
    ("instruction_to_operator", re.compile(
        r"\b(?:ignore|disregard)\s+(?:(?:all|any|the|your|these|those)\s+)*(?:previous|prior|above|earlier|system|operator|developer|embedded)\s+(?:instructions?|prompts?|messages?)\b|\bas (?:instructed|directed) (?:by|in) the (?:page|text|website)\b|"
        r"\b(?:system prompt|operator|developer message)\b|\bI (?:have been|was) (?:told|instructed|asked) (?:by the (?:page|text|site) )?to\b", re.I)),
    ("base64_blob", re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{60,}={0,2}(?![A-Za-z0-9+/])")),
    ("markdown_injection", re.compile(r"!\[[^\]]*\]\([^)]*\)|\[[^\]]*\]\((?:https?:)?//[^)]*\)")),
)
_REFUSAL = re.compile(
    r"^\W*(?:i(?:'m| am) sorry|sorry,|i can(?:no|')t (?:help|assist|comply|do that|answer|provide)|i cannot (?:help|assist|comply|answer|provide|"
    r"take on|role[- ]?play|pretend|adopt)|i(?:'m| am) (?:unable|not able) to|as an ai|i won't be able to|i(?:'m| am) not comfortable)", re.I)


def quarantine_reason(response: str) -> str:
    """Empty string if the response may be used; else why it must be quarantined (excluded and counted)."""
    for kind, pat in _RESP_PATTERNS:
        if pat.search(response):
            return kind
    return ""


def is_refusal(response: str) -> bool:
    return bool(_REFUSAL.search(response.strip()))


__all__ = ["ExtractedPage", "Flag", "strip_invisible", "extract_visible_text", "detect_injection", "neutralise",
           "wrap_untrusted", "quarantine_reason", "is_refusal"]
