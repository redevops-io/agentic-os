"""Does a Windows security descriptor make a file private to one user? Pure string logic over an SDDL string, so it is
tested on every OS; ``_win32`` reads the real descriptor and ``files.private_file_problem`` applies this."""
from __future__ import annotations

import re

_ALIASES = {"SY": "S-1-5-18", "BA": "S-1-5-32-544", "CO": "S-1-3-0", "OW": "S-1-3-4", "WD": "S-1-1-0", "BU": "S-1-5-32-545",
            "AU": "S-1-5-11", "IU": "S-1-5-4", "NU": "S-1-5-2", "AN": "S-1-5-7"}
# Principals that can always read a user's files anyway (the system, the machine's administrators) or that name the
# file's own owner. They are the Windows counterpart of "root" in a POSIX 0600 check and are not counted as exposure.
_ALWAYS_ALLOWED = frozenset({"S-1-5-18", "S-1-5-32-544", "S-1-3-0", "S-1-3-4"})
_ALLOW_ACE_TYPES = frozenset({"A", "OA", "XA", "ZA"})


def _sid(token: str, resolve=None) -> str:
    """A trustee as a SID string. SDDL writes well-known accounts as two letters (``SY``, ``WD``, and ``LA`` for the machine's
    built-in Administrator, which can be the running user itself); ``resolve`` turns any other abbreviation into the real SID, and an
    abbreviation it cannot resolve stays as written and so counts as a foreign principal."""
    if token.startswith("S-") or token in _ALIASES:
        return _ALIASES.get(token, token)
    if resolve is not None:
        try:
            return resolve(token) or token
        except OSError:
            return token
    return token


_TRUSTEE = r"(?:S-[0-9-]+|[A-Z]{2})"


def _aces(dacl: str) -> "list[str] | None":
    """The top-level ACE strings of a ``D:`` section (an ``S:`` audit section, if any, holds only audit ACEs, which grant nothing), counting parentheses so an ACE with a
    conditional expression (nested parentheses) cannot hide the ACEs after it. None when the parentheses do not balance."""
    out, depth, start = [], 0, 0
    for i, ch in enumerate(dacl):
        if ch == "(":
            if depth == 0:
                start = i + 1
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return None
            if depth == 0:
                out.append(dacl[start:i])
    return out if depth == 0 else None


def dacl_problem(sddl: str, user_sid: str, resolve=None) -> str | None:
    """Why this owner+DACL is not private to ``user_sid``, or None. Private means: a DACL exists (a missing one grants
    everyone full access), the owner is the user, the administrators or the system, and every ACE that grants access
    (inherit-only ACEs do not apply to the object itself) names the user, the system, the administrators or the
    owner. Anything else (Everyone, Users, Authenticated Users, another account, a group) is exposure, and a descriptor
    that cannot be parsed is refused."""
    m = re.match(rf"O:({_TRUSTEE})", sddl)
    if not m:
        return "its owner could not be read"
    owner = _sid(m.group(1), resolve)
    if owner not in _ALWAYS_ALLOWED | {user_sid}:
        return f"is owned by {owner}, not by the running user"
    rest = sddl[m.end():]
    g = re.match(rf"G:({_TRUSTEE})", rest)
    if g:
        rest = rest[g.end():]
    d = re.match(r"D:", rest)
    if not d:
        return "has no DACL (every account has full access)"
    body = rest[d.end():]
    first = body.find("(")
    aces = _aces(body[first:]) if first >= 0 else []
    if aces is None:
        return "has a DACL that could not be parsed"
    for ace in aces:
        f = ace.split(";")
        if len(f) < 6 or f[0] not in _ALLOW_ACE_TYPES:
            continue
        if "IO" in re.findall(r"[A-Z]{2}", f[1]):
            continue
        who = _sid(f[5], resolve)
        if who not in _ALWAYS_ALLOWED | {user_sid}:
            return f"grants access to {who}, which is not the running user"
    return None
