"""A SECOND, independently worded local anchor pack for ``purchase_intent`` and ``trust`` (sensitivity only).

Used by ``anchor_sensitivity`` to check whether rankings depend on the exact anchor wording. Like the primary
packs these are local, not the paper's (unpublished) anchors; they are never mixed with the primary packs.
"""
from __future__ import annotations

from .anchors import AnchorPack

PURCHASE_INTENT_ALT = AnchorPack("purchase_intent", "local-v1-paraphrase", (
    ("Buying this is out of the question for me.", "I'm inclined not to buy it.", "Honestly I could take it or leave it.",
     "I'm inclined to buy it.", "Buying this is an easy yes for me."),
    ("No chance I'd put money into this.", "Spending money on this seems unlikely.", "I haven't made up my mind about spending on it.",
     "Spending money on this seems likely.", "I'm ready to sign up and pay."),
    ("I'd walk away from this offer.", "I'd lean toward walking away.", "I'm ambivalent about taking this offer.",
     "I'd lean toward taking this offer.", "I'd take this offer immediately."),
    ("This wouldn't earn a place in my budget.", "This would struggle to earn a place in my budget.",
     "Whether this earns a place in my budget is unclear.", "This would likely earn a place in my budget.",
     "This clearly earns a place in my budget."),
    ("I won't be purchasing this.", "I probably won't be purchasing this.", "My purchasing decision here is up in the air.",
     "I will probably be purchasing this.", "I will be purchasing this."),
    ("Nothing here makes me want to become a customer.", "Little here makes me want to become a customer.",
     "I'm lukewarm on becoming a customer.", "Enough here makes me want to become a customer.",
     "I'm eager to become a customer."),
))

TRUST_ALT = AnchorPack("trust", "local-v1-paraphrase", (
    ("I don't believe a word of this.", "I doubt much of this.", "I'm unsure how much of this to believe.",
     "I believe most of this.", "I believe all of this."),
    ("This vendor strikes me as dishonest.", "This vendor strikes me as somewhat shaky.",
     "I can't judge this vendor's honesty.", "This vendor strikes me as honest.", "This vendor strikes me as thoroughly dependable."),
    ("Nothing here backs up the claims.", "Little here backs up the claims.", "Some here backs up the claims, some doesn't.",
     "Much here backs up the claims.", "Everything here backs up the claims."),
    ("I wouldn't stake anything important on this.", "I'd think twice before staking anything important on this.",
     "I can't say if I'd stake anything important on this.", "I'd be fine staking something important on this.",
     "I'd stake our critical work on this."),
    ("This reads as pure puffery.", "This reads as mostly puffery.", "This reads as part puffery, part fact.",
     "This reads as mostly fact.", "This reads as entirely fact."),
    ("Handing over my information here would worry me a lot.", "Handing over my information here would worry me somewhat.",
     "I have no firm feeling about handing over my information here.", "Handing over my information here seems acceptable.",
     "Handing over my information here seems entirely safe."),
))

ALT_PACKS = {"purchase_intent": PURCHASE_INTENT_ALT, "trust": TRUST_ALT}
for _p in ALT_PACKS.values():
    _p.validate()

__all__ = ["PURCHASE_INTENT_ALT", "TRUST_ALT", "ALT_PACKS"]
