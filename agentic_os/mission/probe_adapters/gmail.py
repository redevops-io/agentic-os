"""Gmail reconciliation probe for `email.message.send`.

What Gmail documents (developers.google.com/workspace/gmail/api): `users.messages.send` takes a base64url RFC 2822 message and returns `{id, threadId}`. It documents NO
idempotency mechanism, does not say whether a client-supplied `Message-ID` header is kept, and does not say how soon a sent message is searchable. `users.messages.list` accepts a
search `q` (`in:sent`, `to:`, `subject:`, `after:<epoch seconds>`); `rfc822msgid:` is a Gmail search operator but the API page does not list it.

So the probe never takes a search hit at face value:
  * every hit is fetched (`format=metadata`) and its `Subject`, `To` and `internalDate` are checked against the reservation: Gmail's `subject:` matches a phrase anywhere in a subject;
  * when the reservation carries a `message_id` (the sender set a `Message-ID` header) the probe searches `rfc822msgid:` and CONFIRMS only if the stored message's own `Message-ID` header
    equals it: that is identification, whatever Gmail does with the header in general;
  * without a `message_id`, one verified message to the same recipient with the same subject since the call is a CANDIDATE (see `_common`): INCONCLUSIVE by default, naming it;
    two or more may include a duplicate; none is INCONCLUSIVE (the index delay is undocumented, so "not found" is never "not sent").
No Gmail probe can return an authoritative negative. Today `redevops-connectors` builds the message without a Message-ID, so the candidate path is the one in use until the sender adds one.
"""
from __future__ import annotations

from typing import Any, Optional

from ..probes import ProbeContext, ProbeOutcome, ProbeResult, Qualification
from ._common import Http, ProbeBase, SKEW_S, epoch, httpx_get

QUALIFICATION = Qualification("gmail", eventually_consistent=True, visibility_window_s=None, authoritative_negative=False)
READ_PATHS = ("/users/me/messages",)
REF_KEYS = ("to", "subject", "message_id")
MAX_CANDIDATES = 10


def default_http(access_token: str, base_url: str = "https://gmail.googleapis.com/gmail/v1", timeout: float = 10.0) -> Http:
    """GET-only transport, restricted to the message list/get paths (the `gmail.readonly` scope is enough for it)."""
    return httpx_get(base_url, {"Authorization": f"Bearer {access_token}"}, READ_PATHS, timeout, one_segment=True)


def derive_ref(inputs: dict) -> dict:
    """The sender's fields that identify the message (what `redevops-connectors`' `email.message.send` takes)."""
    out = {}
    to = inputs.get("to")
    if isinstance(to, (list, tuple)):
        to = to[0] if len(to) == 1 else None            # several recipients cannot be matched on one `to:`: unsupported rather than guessed
    for k, v in (("to", to), ("subject", inputs.get("subject")), ("message_id", inputs.get("message_id"))):
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()
    return out


def _addr(s: str) -> str:
    s = s.strip().lower()
    if "<" in s and ">" in s:
        s = s[s.rindex("<") + 1:s.rindex(">")]
    return s


def _headers(msg: dict) -> dict:
    return {str(h.get("name", "")).lower(): str(h.get("value", "")) for h in ((msg.get("payload") or {}).get("headers") or []) if isinstance(h, dict)}


def _q_text(s: str) -> str:
    return s.replace('"', " ").replace("\\", " ")


class GmailProbes(ProbeBase):
    def __init__(self, http: Http, *, accept_unique_match: bool = False) -> None:
        """`accept_unique_match`: treat exactly one verified message (same recipient, same subject, sent since the call) as proof it was sent. Off by default; see the module doc."""
        super().__init__(http, accept_unique_match=accept_unique_match)

    def send(self, ctx: ProbeContext) -> ProbeResult:
        ref = ctx.effect_ref or {}
        to, subject, mid = ref.get("to"), ref.get("subject"), ref.get("message_id")
        if not (isinstance(to, str) and to and isinstance(subject, str) and subject):
            return ProbeResult(ProbeOutcome.UNSUPPORTED, detail="the reservation does not say who the message went to and its subject: it cannot be looked up")
        since = ctx.dispatched_at - SKEW_S
        if isinstance(mid, str) and mid:
            return self._by_message_id(ctx, mid.strip().strip("<>"), since)
        q = f'in:sent to:{_q_text(to)} subject:"{_q_text(subject)}" after:{int(since)}'
        found = self._list(q)
        if isinstance(found, ProbeResult):
            return found
        hits: list = []
        for mref in found[:MAX_CANDIDATES]:
            msg = self._fetch(str(mref.get("id", "")))
            if isinstance(msg, ProbeResult):
                return msg
            h = _headers(msg or {})
            sent = epoch(msg.get("internalDate")) if msg else None
            sent = sent / 1000.0 if sent is not None else None
            if (h.get("subject", "").strip() == subject.strip() and _addr(to) in [_addr(x) for x in h.get("to", "").split(",")]
                    and sent is not None and sent >= since):
                hits.append(msg)
        if len(hits) == 1:
            m = hits[0]
            return self._unique_candidate(ctx.now, f"message {m.get('id')} to {to}, subject {subject[:60]!r}", str(m.get("id") or ""), "sent message",
                                          {"id": m.get("id"), "threadId": m.get("threadId")})
        if len(hits) > 1:
            return ProbeResult(ProbeOutcome.INCONCLUSIVE, detail=f"{len(hits)} sent messages to the same recipient with the same subject since the call: one may be a duplicate; a person must look")
        return ProbeResult(ProbeOutcome.INCONCLUSIVE, detail="no matching sent message found (Gmail documents no search-index delay: not proof the message was never sent)")

    # ── helpers ──
    def _list(self, q: str):
        got = self._get("/users/me/messages", {"q": q, "maxResults": MAX_CANDIDATES})
        if isinstance(got, ProbeResult):
            return got
        code, body, headers = got
        bad = self._bad_status(code, headers)
        if bad is not None:
            return bad
        msgs = (body or {}).get("messages") if isinstance(body, dict) else None
        return [m for m in (msgs or []) if isinstance(m, dict) and m.get("id")]

    def _fetch(self, message_id: str):
        got = self._get(f"/users/me/messages/{message_id}", {"format": "metadata", "metadataHeaders": ["Subject", "To", "Message-ID"]})
        if isinstance(got, ProbeResult):
            return got
        code, body, headers = got
        if code == 404:
            return {}                                      # deleted between the list and the get: it is not evidence of anything
        bad = self._bad_status(code, headers)
        if bad is not None:
            return bad
        return body if isinstance(body, dict) else {}

    def _by_message_id(self, ctx: ProbeContext, mid: str, since: float) -> ProbeResult:
        found = self._list(f"rfc822msgid:{_q_text(mid)}")
        if isinstance(found, ProbeResult):
            return found
        for mref in found[:MAX_CANDIDATES]:
            msg = self._fetch(str(mref.get("id", "")))
            if isinstance(msg, ProbeResult):
                return msg
            if _headers(msg or {}).get("message-id", "").strip().strip("<>") == mid:
                return ProbeResult(ProbeOutcome.CONFIRMED_EXECUTED,
                                   evidence=self._evidence(f"gmail message {msg.get('id')} carries Message-ID {mid}", ctx.now),
                                   provider_ref=str(msg.get("id") or ""), observed_at=ctx.now,
                                   result={"id": msg.get("id"), "threadId": msg.get("threadId"), "reconciled": True},
                                   detail="a stored message carries the Message-ID this request set")
        return ProbeResult(ProbeOutcome.INCONCLUSIVE, detail="no stored message carries that Message-ID (Gmail may not keep a client-supplied one, or it is not searchable yet): not proof the message was never sent")


def register_gmail(registry, http: Http, *, capability: str = "email.message.send", accept_unique_match: bool = False) -> GmailProbes:
    p = GmailProbes(http, accept_unique_match=accept_unique_match)
    registry.register(capability, p.send, QUALIFICATION, ref_keys=(), ref_derive=derive_ref)
    return p
