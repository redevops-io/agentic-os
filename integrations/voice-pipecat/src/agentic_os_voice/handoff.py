"""Distress and emergency language: route to a person, make no promise.

A deliberately small, conservative, English-only phrase screen that runs on the transcript BEFORE a model sees it.
It is a safety net, not a classifier of risk: it will miss phrasings, languages, accents and speech-to-text errors,
and it will sometimes fire when nobody is in danger (that direction is intended). Do not describe it as detecting
emergencies. A deployment that serves people who may be in danger needs its own reviewed policy and procedures.
"""
from __future__ import annotations

import re
from typing import Optional

CATEGORIES = {
    "emergency": r"\b(emergency|ambulance|call (an? )?(ambulance|911|999|112)|heart attack|can'?t breathe|not breathing|"
                 r"overdos\w*|bleeding (badly|heavily)|on fire|being attacked|someone is (hurt|dying))\b",
    "self_harm": r"\b(kill myself|end my life|suicid\w*|want to die|hurt myself|harm myself|don'?t want to (live|be alive))\b",
    "violence": r"\b((he|she|they) (is|are|will) (going to )?(hurt|kill)(ing)? (me|us)|i am (in danger|being abused)|domestic violence|"
                r"threaten(ed|ing)? (to kill|my life))\b",
    "distress": r"\b(i(\'m| am) (scared|terrified|panicking)|please help me|i need help (right )?now)\b",
}
_COMPILED = {k: re.compile(v, re.IGNORECASE) for k, v in CATEGORIES.items()}

#: The only thing spoken on a match. It requests a handoff and refers the caller to emergency services in general
#: terms. It does not say a person is on the way, does not say help is coming, and gives no time.
HANDOFF_SCRIPT = ("I may not be the right help for this. If you or anyone else is in immediate danger, please contact your local "
                  "emergency services now. I am requesting that a person on our team contact you, but I cannot promise when "
                  "that will happen.")


def classify(text: str) -> Optional[str]:
    """The first matching category, or None. Input is a transcript fragment (untrusted)."""
    if not isinstance(text, str):
        return None
    t = text[:2000]
    for name, rx in _COMPILED.items():
        if rx.search(t):
            return name
    return None
