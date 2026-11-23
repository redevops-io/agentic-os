"""Anchor packs for SSR: LOCAL, independently authored, versioned, one pack per CONSTRUCT.

IMPORTANT (fidelity statement): the SSR paper (arXiv:2510.08338) describes six anchor sets of five statements
(Appendix C.1) but does not publish them, and the authors' reference package ships only two toy example sets.
The packs below are therefore NOT the paper's anchors. They were written independently for this project, follow
the paper's stated design (the lowest anchor expresses unlikely/none, the middle indifference or uncertainty, the
highest strong intent; the two intermediate anchors sit between their neighbours; short, generic,
domain-independent sentences; six "similar but not identical" sets) and carry their own version and content
hash. Any result produced with them is a result of THIS pack and must be labelled with ``pack.pack_id``.

One pack per construct: purchase intent, trial/pilot intent, relevance and trust are different quantities and
must not reuse each other's anchors (plan Rev 2 §4.4). The packs were designed for first-person statements by a
synthetic respondent about the thing they were shown.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Dict, Tuple

AnchorSet = Tuple[str, str, str, str, str]          # Likert 1 .. 5


@dataclass(frozen=True)
class AnchorPack:
    construct: str
    version: str
    sets: Tuple[AnchorSet, ...]
    language: str = "en"

    @property
    def pack_id(self) -> str:
        """Stable identifier: ``<construct>/<version>/<first 12 hex of the content hash>``."""
        return f"{self.construct}/{self.version}/{self.content_hash[:12]}"

    @property
    def content_hash(self) -> str:
        blob = json.dumps({"construct": self.construct, "version": self.version, "language": self.language,
                           "sets": [list(s) for s in self.sets]}, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def validate(self) -> None:
        if not self.sets:
            raise ValueError("an anchor pack needs at least one set")
        seen = set()
        for k, s in enumerate(self.sets):
            if len(s) != 5 or any((not isinstance(t, str)) or not t.strip() for t in s):
                raise ValueError(f"{self.construct} set {k}: five non-empty statements required")
            if len(set(s)) != 5:
                raise ValueError(f"{self.construct} set {k}: duplicate statements inside a set")
            for t in s:
                if t in seen:
                    raise ValueError(f"{self.construct}: statement reused across sets: {t!r}")
                seen.add(t)

    def flat_texts(self) -> Tuple[str, ...]:
        return tuple(t for s in self.sets for t in s)


PURCHASE_INTENT = AnchorPack("purchase_intent", "local-v1", (
    ("It's very unlikely that I would buy this.", "I probably wouldn't buy this.",
     "I might or might not buy this; I can't say.", "I would probably buy this.",
     "It's very likely that I would buy this."),
    ("I have no interest in buying this.", "I'm leaning against buying it.",
     "I'm undecided about buying it.", "I'm leaning toward buying it.", "I definitely want to buy this."),
    ("There is almost no chance I'd purchase this.", "I doubt I would purchase this.",
     "I'm neutral; buying it is a coin flip.", "I'd likely purchase this.",
     "I'm almost certain I'd purchase this."),
    ("This is not something I would pay for.", "I'd be reluctant to pay for this.",
     "I'm not sure whether this is worth paying for.", "I'd be willing to pay for this.",
     "I'd gladly pay for this right away."),
    ("I would pass on this without a second thought.", "I'd most likely pass on this.",
     "I could go either way on this.", "I'd most likely go for this.",
     "I would choose this without hesitation."),
    ("I can't see myself ever buying this.", "It's improbable that I'd buy this.",
     "I feel indifferent about buying this.", "It's quite probable that I'd buy this.",
     "I can easily see myself buying this soon."),
))

TRIAL_INTENT = AnchorPack("trial_intent", "local-v1", (
    ("We would not run a trial of this.", "We'd probably not trial this.",
     "We might consider a trial, but we're unsure.", "We would probably run a trial of this.",
     "We would definitely start a trial of this."),
    ("I would not recommend that we pilot this.", "I'd hesitate to recommend a pilot.",
     "I'm unsure whether to recommend a pilot.", "I'd recommend we pilot this.",
     "I'd strongly recommend that we pilot this now."),
    ("This will not make it onto our pilot shortlist.", "This is unlikely to make our pilot shortlist.",
     "This might make our pilot shortlist; it depends.", "This will probably make our pilot shortlist.",
     "This is at the top of our pilot shortlist."),
    ("I wouldn't sign up to try this.", "I'm reluctant to sign up to try this.",
     "I'm on the fence about signing up to try this.", "I'd sign up to try this.",
     "I'd sign up to try this today."),
    ("We have no appetite for a proof of concept here.", "We have little appetite for a proof of concept here.",
     "Our appetite for a proof of concept is mixed.", "We have good appetite for a proof of concept here.",
     "We want to start a proof of concept immediately."),
    ("I would not ask for a demo of this.", "I'm unlikely to ask for a demo.",
     "I may or may not ask for a demo.", "I'd likely ask for a demo.", "I'd book a demo straight away."),
))

RELEVANCE = AnchorPack("relevance", "local-v1", (
    ("This has nothing to do with what I need.", "This barely relates to what I need.",
     "This is somewhat related to what I need.", "This largely matches what I need.",
     "This is exactly what I need."),
    ("This is irrelevant to my situation.", "This is mostly irrelevant to my situation.",
     "This is partly relevant to my situation.", "This is clearly relevant to my situation.",
     "This is highly relevant to my situation."),
    ("This isn't aimed at someone like me.", "This seems aimed more at others than at me.",
     "I can't tell whether this is aimed at someone like me.", "This seems aimed at someone like me.",
     "This is clearly made for someone like me."),
    ("This does not solve any problem I have.", "This solves a problem I hardly have.",
     "This might solve a problem I have.", "This solves a problem I have.", "This solves my biggest problem."),
    ("I searched for something different from this.", "This is not quite what I searched for.",
     "This is roughly what I searched for.", "This is close to what I searched for.",
     "This is precisely what I searched for."),
    ("The page speaks to none of my concerns.", "The page speaks to few of my concerns.",
     "The page speaks to some of my concerns.", "The page speaks to most of my concerns.",
     "The page speaks to all of my concerns."),
))

TRUST = AnchorPack("trust", "local-v1", (
    ("I do not trust these claims at all.", "I'm skeptical of these claims.",
     "I can't tell whether to trust these claims.", "I mostly trust these claims.",
     "I fully trust these claims."),
    ("This company seems untrustworthy to me.", "This company seems a bit unreliable to me.",
     "I have no clear view of this company's reliability.", "This company seems reliable to me.",
     "This company seems very reliable to me."),
    ("I see no credible evidence behind this.", "The evidence behind this looks thin.",
     "The evidence behind this is mixed.", "The evidence behind this looks solid.",
     "The evidence behind this is compelling."),
    ("I would not rely on this for anything important.", "I'd hesitate to rely on this for important work.",
     "I'm unsure whether I could rely on this for important work.", "I could rely on this for important work.",
     "I'd rely on this for our most important work."),
    ("This feels like marketing hype with nothing behind it.", "This feels mostly like marketing hype.",
     "This feels like a mix of substance and hype.", "This feels mostly substantive.",
     "This feels thoroughly substantiated."),
    ("I would not feel safe handing this my data.", "I'd feel uneasy handing this my data.",
     "I'm not sure how I'd feel handing this my data.", "I'd feel fairly safe handing this my data.",
     "I'd feel completely safe handing this my data."),
))

PACKS: Dict[str, AnchorPack] = {p.construct: p for p in (PURCHASE_INTENT, TRIAL_INTENT, RELEVANCE, TRUST)}
for _p in PACKS.values():
    _p.validate()


def get_pack(construct: str) -> AnchorPack:
    try:
        return PACKS[construct]
    except KeyError:
        raise KeyError(f"no anchor pack for construct {construct!r}; known: {sorted(PACKS)}") from None


__all__ = ["AnchorPack", "AnchorSet", "PURCHASE_INTENT", "TRIAL_INTENT", "RELEVANCE", "TRUST", "PACKS", "get_pack"]
