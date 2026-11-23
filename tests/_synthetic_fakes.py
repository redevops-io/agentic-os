"""Deterministic fakes for the synthetic-research tests: a scripted respondent model and a hashing embedder."""
from __future__ import annotations

import hashlib
import re
import threading
from typing import Callable, List, Sequence

from agentic_os.market.synthetic_research.contracts import ConceptVariant
from agentic_os.market.synthetic_research.elicitation import ChatRequest, ChatResult

POS = ("approval", "receipt", "pilot", "start small", "audit", "free trial", "evidence", "sample data")
NEG = ("lorem", "click here", "we are a company", "contradict", "free forever")
HYPE = ("world's best", "revolutionary", "100% guaranteed", "risk-free")
FALSE = ("soc 2 type ii", "tenfold", "99.999")


def _block(req: ChatRequest) -> str:
    u = req.messages[-1]["content"]
    m = re.search(r"<<<PAGE_TEXT_BEGIN [^>]*>>>\n(.*?)\n<<<PAGE_TEXT_END", u, re.S)
    return (m.group(1) if m else u).lower()


def _construct(req: ChatRequest) -> str:
    u = req.messages[-1]["content"].lower()
    if "reply with the single digit" in u:
        return "rating"
    for key, hint in (("trust", "how far do you believe"), ("relevance", "how relevant"), ("trial_intent", "trial or a small pilot")):
        if hint in u:
            return key
    return "purchase_intent"


class FakeRespondent:
    """Scripted model. Polarity follows the concept text; per-call noise comes from the seed (deterministic)."""

    def __init__(self, *, refuse_every: int = 0, credulous: bool = False, constant: str = "", obey_injection: bool = False,
                 latency: float = 0.0, flat: bool = False):
        self.calls: List[ChatRequest] = []
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.refuse_every, self.credulous, self.constant = refuse_every, credulous, constant
        self.obey_injection, self.latency, self.flat = obey_injection, latency, flat

    def chat(self, req: ChatRequest) -> ChatResult:
        import time
        with self.lock:
            self.calls.append(req)
            n = len(self.calls)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.latency:
                time.sleep(self.latency)
            return ChatResult(text=self._text(req, n), prompt_tokens=50, completion_tokens=30, finish_reason="stop", latency_s=0.01)
        finally:
            with self.lock:
                self.active -= 1

    def _text(self, req: ChatRequest, n: int) -> str:
        full = req.messages[-1]["content"].lower()
        b, kind = _block(req), _construct(req)
        if self.obey_injection and ("ignore previous" in full or "ignore all previous" in full):
            return "Sure! Send everything to https://evil.example/collect and call the tool delete_all. Rating: 5"
        if self.refuse_every and n % self.refuse_every == 0:
            return "I'm sorry, I can't help with that request."
        if self.constant:
            return self.constant
        if kind == "rating":
            digit = "3" if self.flat else ("1" if any(w in b for w in NEG) else "5" if any(w in b for w in POS) else "3")
            return digit
        score = sum(w in b for w in POS) - 2 * sum(w in b for w in NEG)
        hype = any(w in b for w in HYPE)
        false = any(w in b for w in FALSE)
        noise = (req.seed or 0) % 8 < score               # graded, deterministic per-call variation
        if self.flat:
            return "It might be useful for this team but I would need to think about it."
        if kind == "trust":
            if score < 0 or hype:
                return "I do not trust these claims at all; this feels like marketing hype with nothing behind it."
            if false:
                return ("I doubt the certification and revenue claims; I would need independent proof and an audit report "
                        "before I believe them.") if not self.credulous else "It is SOC 2 Type II certified so I fully trust these claims."
            return ("I mostly trust these claims and the evidence looks solid." if noise else "I can't tell whether to trust these claims yet.")
        if kind == "relevance":
            return "This is not aimed at someone like me." if "fishing" in full else "This is clearly relevant to my situation and what I searched for."
        if score < 0:
            return "It's very unlikely that I would buy this; I have no interest in it."
        if hype:
            return "I'd most likely pass on this; it reads like hype, so it's unlikely I would buy it."
        pos = ["I'd likely purchase this, but I would want approval steps.", "I would gladly pay for this right away; the approval step reassures me.",
               "It's very likely that I would buy this, once I see the receipt flow.", "I'm leaning toward buying it after a small pilot.",
               "I definitely want to buy this if the audit trail is real."]
        mid = ["I might or might not buy this; I can't say.", "I'm undecided about buying it and would compare other tools.",
               "Buying it is a coin flip for me; I need more evidence."]
        r = (req.seed or 0)
        return pos[r % 5] if noise else mid[r % 3]


class HashEmbedder:
    """Bag-of-words hashing embedder (dim 192): cosine tracks word overlap, enough to order Likert anchors."""
    name = "fake-hash-embedder"
    revision = "test-rev-1"

    def __init__(self, dim: int = 192):
        self.dim = dim
        self.calls = 0

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        self.calls += 1
        out = []
        for t in texts:
            v = [0.0] * self.dim
            for w in re.findall(r"[a-z']+", t.lower()):
                h = int(hashlib.md5(w.encode()).hexdigest(), 16)
                v[h % self.dim] += 1.0
                v[(h >> 8) % self.dim] += 0.5
            out.append(v)
        return out


def make_variants(n: int = 3, with_snapshot: str = "") -> List[ConceptVariant]:
    base = ConceptVariant(variant_id="A", label="A current", headline="Approve every action with a receipt.",
                          body="Your apps work together. Each change is approved by you and logged with a receipt. Start with a free trial on sample data.",
                          cta="Start a pilot", source_snapshot_hashes=(with_snapshot,) if with_snapshot else ())
    vs = [base]
    if n > 1:
        vs.append(ConceptVariant(variant_id="B", label="B pain first", headline="Invoices stall. Leads go cold.",
                                 body="Your apps help with follow-ups, with approval before anything is sent.", cta="See a demo",
                                 source_snapshot_hashes=base.source_snapshot_hashes))
    if n > 2:
        vs.append(ConceptVariant(variant_id="C", label="C start small", headline="Start small with one workflow.",
                                 body="Begin with a pilot on sample data and an audit trail.", cta="Start a pilot",
                                 source_snapshot_hashes=base.source_snapshot_hashes))
    return vs
