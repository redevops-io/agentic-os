"""Unsupported-claim detector (deterministic, no LLM).

A respondent answer sometimes asserts product facts the supplied page text never stated ("it integrates with
Salesforce", "they are SOC 2 certified", "it costs $49"). This module flags such CLAIM CANDIDATES by comparing
claim-like tokens in sentences that attribute something to the product/vendor against the supplied corpus (the
page snapshot text, the concept text and the persona description). Questions, hedges and the respondent's own
needs ("I would need to see a SOC 2 report") are NOT claims and are skipped.

It is a heuristic with a measurable false-positive rate: :func:`evaluate_detector` scores it on labelled cases
(see tests/test_synthetic_research_personas.py for the crafted corpus and the study reports for an audit of real
flags). It never edits or removes a response; it only flags.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Sequence, Tuple

from .contracts import split_sentences

_ATTRIB = re.compile(
    r"\b(?:it|it's|they|they're|this|the (?:tool|platform|product|system|software|company|vendor|page|site|website|"
    r"service|offer|runtime|stack)|their|its)\b", re.I)
_VERB = re.compile(
    r"\b(?:costs?|charges?|supports?|integrates?|connects?|includes?|offers?|provides?|has|have|comes? with|claims?|"
    r"says?|promises?|guarantees?|certified|compliant|saves?|reduces?|increases?|handles?|runs? on|works? with|"
    r"is|are|does|do|will)\b", re.I)
_HEDGE = re.compile(
    r"\?|\b(?:if|whether|unless|assuming|would|could|might|may|need(?:s)? to (?:see|know|verify|confirm)|want(?:s)? to (?:know|see)|"
    r"should|ask(?:ed|ing)?|prove|proof|evidence of|not (?:say|state|mention|clear|specified)|doesn't (?:say|state|mention|"
    r"specify)|does not (?:say|state|mention|specify)|no (?:mention|price|pricing|information)|unclear|missing)\b", re.I)
_FEATURE = re.compile(
    r"\b(?:integrates? with|connects? (?:to|with)|supports?|includes?|offers?|provides?|comes with|works? with|runs? on)\s+"
    r"([^.,;:!?]{3,80})", re.I)
_NUM = re.compile(r"(?<![\w.])(?:[$€£]\s?\d[\d,]*(?:\.\d+)?(?:\s?[kKmM])?|\d[\d,]*(?:\.\d+)?\s?(?:%|percent|x\b|times\b|"
                  r"(?:usd|eur|dollars|euros)\b|(?:k|m)\b|days?\b|hours?\b|minutes?\b|weeks?\b|months?\b|years?\b|users?\b|"
                  r"seats?\b|employees?\b|integrations?\b|apps?\b))", re.I)
_ENTITY = re.compile(r"(?<![.!?]\s)(?<!^)\b(?:[A-Z][a-zA-Z0-9]+(?:\s[A-Z0-9][a-zA-Z0-9]*)*|[A-Z]{2,}(?:\s?\d+)?(?:\s?Type\s?[IV]+)?)\b")
_CERT = re.compile(r"\b(?:SOC\s?2(?:\s?Type\s?(?:I{1,2}|1|2))?|ISO\s?\d{4,5}|HIPAA|GDPR|PCI(?:[- ]DSS)?|FedRAMP|FIPS(?:\s?\d+)?|CCPA|"
                   r"NIST(?:\s?\d+)?|ISO/IEC\s?\d+)\b", re.I)
_GENERIC_CAPS = set("""I I'm I'd I'll I've AI API UI UX IT CRM ERP PSA RMM SaaS SMB CEO CTO CIO CISO CFO VP Monday Tuesday Wednesday
Thursday Friday Saturday Sunday January February March April May June July August September October November December
Yes No OK Okay""".split())
_STOP = set("""about above after again also always another because been before being between both could does doing down during
each from have having here into just more most much only other over same should some such than that their them then there
these they this those through under until very were what when where which while with would your yours""".split())


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9$%€£ ]+", " ", s.lower())).strip()


def _stems(s: str) -> set:
    return {w[:5] for w in re.findall(r"[a-z]{4,}", s.lower()) if w not in _STOP}


@dataclass(frozen=True)
class ClaimFlag:
    kind: str                 # number | price | entity | certification | feature
    text: str
    sentence: str


def _in_corpus(token: str, corpus_norm: str) -> bool:
    t = _norm(token)
    digits = re.sub(r"\D", "", token)
    if digits and not re.search(r"[a-z]{2,}", t):
        return bool(re.search(r"(?<!\d)" + re.escape(digits) + r"(?!\d)", re.sub(r"[,]", "", corpus_norm)))
    return t in corpus_norm


def find_unsupported(response: str, supported_texts: Sequence[str]) -> List[ClaimFlag]:
    corpus = " ".join(supported_texts)
    cn = _norm(corpus)
    cstems = _stems(corpus)
    flags: List[ClaimFlag] = []
    for sent in split_sentences(response):
        if _HEDGE.search(sent) or not (_ATTRIB.search(sent) and _VERB.search(sent)):
            continue
        for m in _CERT.finditer(sent):
            if not _in_corpus(m.group(0), cn):
                flags.append(ClaimFlag("certification", m.group(0), sent))
        for m in _NUM.finditer(sent):
            tok = m.group(0)
            if not _in_corpus(tok, cn):
                flags.append(ClaimFlag("price" if re.match(r"[$€£]|.*(usd|eur|dollars|euros)", tok, re.I) else "number", tok, sent))
        for m in _FEATURE.finditer(sent):
            obj = m.group(1)
            st = _stems(obj)
            if st and not (st & cstems):
                flags.append(ClaimFlag("feature", obj.strip(), sent))
        for m in _ENTITY.finditer(sent):
            tok = m.group(0).strip()
            if tok in _GENERIC_CAPS or tok.split()[0] in _GENERIC_CAPS or _CERT.fullmatch(tok):
                continue
            if not _in_corpus(tok, cn):
                flags.append(ClaimFlag("entity", tok, sent))
    seen = set()
    out = []
    for f in flags:
        k = (f.kind, f.text.lower())
        if k not in seen:
            seen.add(k)
            out.append(f)
    return out


def evaluate_detector(cases: Iterable[Tuple[str, Sequence[str], bool]]) -> Dict[str, float]:
    """``cases`` = ``(response, corpus_texts, has_unsupported_claim)``. Returns counts and rates."""
    tp = fp = tn = fn = 0
    for resp, corpus, gold in cases:
        got = bool(find_unsupported(resp, corpus))
        tp += got and gold
        fp += got and not gold
        tn += (not got) and (not gold)
        fn += (not got) and gold
    pos, neg = tp + fn, fp + tn
    return {"tp": tp, "fp": fp, "tn": tn, "fn": fn, "false_positive_rate": fp / neg if neg else float("nan"),
            "recall": tp / pos if pos else float("nan"), "precision": tp / (tp + fp) if tp + fp else float("nan")}


__all__ = ["ClaimFlag", "find_unsupported", "evaluate_detector"]
