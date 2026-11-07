"""Explicit test policy: the only way a non-production runtime may execute a side effect without an owner's envelope.

Authority is enforced the same way in every execution profile: a side-effecting node runs only when the owner's envelope
(``authority.AuthorityGuard``) covers it. The development and test profiles used to skip that check silently. They no
longer do. A runtime with no envelope for a mission refuses side effects unless it was given an
:class:`AuthorityTestPolicy`: a named, immutable, digest-pinned object that

* states which capabilities it lets run (``allow``) and which forward capabilities it lets be compensated (``compensate``),
* is recorded in the mission log (``TestPolicyActive`` once per mission, ``TestPolicyAuthorized`` for each decision it makes),
* is visible in ``MissionRuntime.authority_posture()``, ``explain()`` and the mission receipt, and
* is never honoured in the production profile (constructing a runtime with one there raises ``ProfileError``; an inherited
  class-level default is ignored there).

A test policy is not an envelope: it never overrides a revoked, expired or out-of-scope envelope (those are refusals in every
profile), it does not reserve counters, and it does not stand in for a human approval.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any

#: the pattern that matches every capability
ANY = "*"


@dataclass(frozen=True)
class AuthorityTestPolicy:
    name: str
    reason: str
    allow: tuple[str, ...] = ()              # fnmatch patterns of capabilities allowed to execute
    compensate: tuple[str, ...] = ()         # fnmatch patterns of FORWARD capabilities whose compensation is allowed
    anonymous_operators: bool = False        # approve / reject / resume / compensate (and, at an operator's /invoke, call) without an authenticated principal
    unapproved: tuple[str, ...] = ()         # operator /invoke only: fnmatch patterns of capabilities whose mandatory approval this policy waives

    def __post_init__(self) -> None:
        if not str(self.name).strip() or not str(self.reason).strip():
            raise ValueError("a test policy needs a name and a stated reason")
        object.__setattr__(self, "allow", tuple(str(p) for p in self.allow))
        object.__setattr__(self, "compensate", tuple(str(p) for p in self.compensate))
        object.__setattr__(self, "unapproved", tuple(str(p) for p in self.unapproved))

    @classmethod
    def allow_all(cls, name: str, reason: str, *, anonymous_operators: bool = True, unapproved: bool = False) -> "AuthorityTestPolicy":
        """Every capability may run and be compensated. Deliberately verbose to read at the call site. ``unapproved=True`` also waives
        every capability's mandatory approval at an operator's /invoke (the runtime never reads it)."""
        return cls(name=name, reason=reason, allow=(ANY,), compensate=(ANY,), anonymous_operators=anonymous_operators,
                   unapproved=(ANY,) if unapproved else ())

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AuthorityTestPolicy":
        """Build a policy from a JSON object. Unknown keys are an error (a typo must not silently narrow or widen a policy)."""
        if not isinstance(d, dict):
            raise ValueError("a test policy is a JSON object")
        known = {"name", "reason", "allow", "compensate", "anonymous_operators", "unapproved"}
        extra = sorted(set(d) - known)
        if extra:
            raise ValueError(f"unknown test policy field(s): {', '.join(extra)}")
        for k in ("allow", "compensate", "unapproved"):
            if k in d and (not isinstance(d[k], (list, tuple)) or not all(isinstance(p, str) for p in d[k])):
                raise ValueError(f"test policy {k!r} must be a list of capability patterns")
        return cls(name=str(d.get("name", "")), reason=str(d.get("reason", "")), allow=tuple(d.get("allow", ())),
                   compensate=tuple(d.get("compensate", ())), anonymous_operators=d.get("anonymous_operators", False) is True,
                   unapproved=tuple(d.get("unapproved", ())))

    def permits(self, capability: str) -> bool:
        return any(fnmatchcase(capability, p) for p in self.allow)

    def permits_compensation(self, capability: str) -> bool:
        return any(fnmatchcase(capability, p) for p in self.compensate)

    def waives_approval(self, capability: str) -> bool:
        return any(fnmatchcase(capability, p) for p in self.unapproved)

    def to_record(self) -> dict[str, Any]:
        rec = {"name": self.name, "reason": self.reason, "allow": list(self.allow), "compensate": list(self.compensate),
               "anonymous_operators": bool(self.anonymous_operators)}
        if self.unapproved:                  # present only when used, so the record and digest of an existing policy do not change
            rec["unapproved"] = list(self.unapproved)
        return rec

    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(json.dumps(self.to_record(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
