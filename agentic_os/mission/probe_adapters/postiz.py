"""Postiz reconciliation probe for `content.publish`.

What Postiz documents (docs.postiz.com/public-api): `POST /posts` creates one post per channel and returns the post ids; it documents no idempotency key. `GET /posts?startDate&endDate`
(UTC ISO, optional `customer`) returns `{posts: [{id, content, publishDate, releaseURL, state, error, integration: {id, ...}}]}` with `state` one of QUEUE, PUBLISHED, ERROR, DRAFT. The list
endpoint is limited to 30 requests per hour, so a probe makes exactly ONE request and a 429 is a transient error that honours `Retry-After`. Deleted posts are undocumented.

The probe looks for a post on each requested channel whose content equals ours (HTML and whitespace normalized: Postiz's editor may store markup; this is NOT verified against a live instance)
and whose `publishDate` fits the request (the requested date for `schedule`/`draft`, since the call for `now`). Nothing ties such a post to our request, so, as for every adapter without an
identifier (see `_common`), one match per channel is a CANDIDATE: INCONCLUSIVE by default, naming the post ids; `accept_unique_match=True` opts in to confirming it.
State is read honestly even for a candidate: ERROR is never a success, a `now` post still in QUEUE is "look again", and only the state the requested `type` implies (PUBLISHED for `now`,
QUEUE or PUBLISHED for `schedule`, DRAFT for `draft`) can confirm. A missing post is INCONCLUSIVE, never "not published".
Only a GET reaches Postiz through this probe; the transport refuses any other path.
"""
from __future__ import annotations

import hashlib
import html
import re
from typing import Any

from ..probes import ProbeContext, ProbeOutcome, ProbeResult, Qualification
from ._common import Http, ProbeBase, SKEW_S, epoch, httpx_get, iso_utc

QUALIFICATION = Qualification("postiz", eventually_consistent=True, visibility_window_s=None, authoritative_negative=False)
READ_PATHS = ("/posts",)
WINDOW_S = 3600.0
_OK_STATES = {"now": {"PUBLISHED"}, "schedule": {"QUEUE", "PUBLISHED"}, "draft": {"DRAFT"}}


def default_http(api_key: str, base_url: str = "https://api.postiz.com/public/v1", timeout: float = 10.0) -> Http:
    """GET-only transport restricted to `/posts`. Postiz takes the raw key in `Authorization` (no `Bearer`). Self-hosted: `https://<domain>/api/public/v1`."""
    return httpx_get(base_url, {"Authorization": api_key}, READ_PATHS, timeout)


def normalize(content: Any) -> str:
    s = html.unescape(re.sub(r"<[^>]+>", " ", str(content or "")))
    return re.sub(r"\s+", " ", s).strip()


def content_digest(content: Any) -> str:
    return hashlib.sha256(normalize(content).encode()).hexdigest()


def derive_ref(inputs: dict) -> dict:
    """Scalar identity of a `content.publish` request: a digest of the content (the content itself is not copied into the reservation), the channels, the type and the requested date."""
    ids = inputs.get("integrations") or inputs.get("integration_ids") or []
    joined = ",".join(sorted(str(i) for i in ids)) if isinstance(ids, (list, tuple)) else ""
    out = {"type": str(inputs.get("type") or "now")}
    if inputs.get("content", inputs.get("post")) not in (None, ""):
        out["content_sha256"] = content_digest(inputs.get("content", inputs.get("post")))
    if joined and len(joined) <= 200:
        out["integrations"] = joined
    if inputs.get("date"):
        out["date"] = str(inputs["date"])
    return out


class PostizProbes(ProbeBase):
    def __init__(self, http: Http, *, accept_unique_match: bool = False) -> None:
        super().__init__(http, accept_unique_match=accept_unique_match)

    def publish(self, ctx: ProbeContext) -> ProbeResult:
        ref = ctx.effect_ref or {}
        digest, chans = ref.get("content_sha256"), ref.get("integrations")
        if not (isinstance(digest, str) and digest and isinstance(chans, str) and chans):
            return ProbeResult(ProbeOutcome.UNSUPPORTED, detail="the reservation does not say which content went to which channels: the post cannot be looked up")
        kind = str(ref.get("type") or "now")
        if kind not in _OK_STATES:
            return ProbeResult(ProbeOutcome.UNSUPPORTED, detail=f"unknown post type {kind!r}: it cannot be judged")
        requested = epoch(ref.get("date")) if ref.get("date") else None
        anchor = requested if (requested is not None and kind != "now") else ctx.dispatched_at
        got = self._get("/posts", {"startDate": iso_utc(anchor - WINDOW_S), "endDate": iso_utc(max(anchor, ctx.now) + WINDOW_S)})
        if isinstance(got, ProbeResult):
            return got
        code, body, headers = got
        bad = self._bad_status(code, headers)
        if bad is not None:
            return bad
        posts = body if isinstance(body, list) else ((body or {}).get("posts") if isinstance(body, dict) else None)
        if not isinstance(posts, list):
            return ProbeResult(ProbeOutcome.INCONCLUSIVE, detail="the post list came back in a shape this probe does not read: not evidence either way")
        picked: dict = {}
        for iid in chans.split(","):
            matches = []
            for p in posts:
                if not isinstance(p, dict) or str((p.get("integration") or {}).get("id", "")) != iid or content_digest(p.get("content")) != digest:
                    continue
                pd = epoch(p.get("publishDate"))
                fits = (pd is not None and abs(pd - requested) <= SKEW_S) if (requested is not None and kind != "now") else (pd is not None and pd >= ctx.dispatched_at - SKEW_S)
                if fits:
                    matches.append(p)
            picked[iid] = matches
        missing = [i for i, m in picked.items() if not m]
        multi = [i for i, m in picked.items() if len(m) > 1]
        if missing:
            return ProbeResult(ProbeOutcome.INCONCLUSIVE, detail=f"no matching post yet on channel(s) {', '.join(missing)} (not proof it was not posted)")
        if multi:
            return ProbeResult(ProbeOutcome.INCONCLUSIVE, detail=f"several matching posts on channel(s) {', '.join(multi)}: one may be a duplicate; a person must look")
        chosen = [m[0] for m in picked.values()]
        errored = [p for p in chosen if str(p.get("state", "")).upper() == "ERROR"]
        if errored:
            return ProbeResult(ProbeOutcome.INCONCLUSIVE, provider_ref=str(errored[0].get("id") or ""),
                               detail=f"a matching post exists in state ERROR ({str(errored[0].get('error') or 'no message')[:120]}): a failed post is not a published one; a person must decide")
        bad_state = [p for p in chosen if str(p.get("state", "")).upper() not in _OK_STATES[kind]]
        if bad_state:
            return ProbeResult(ProbeOutcome.INCONCLUSIVE, provider_ref=str(bad_state[0].get("id") or ""),
                               detail=f"a matching post is in state {bad_state[0].get('state')!r}, not yet what a {kind!r} request produces ({'/'.join(sorted(_OK_STATES[kind]))}): look again later")
        ids = [str(p.get("id") or "") for p in chosen]
        return self._unique_candidate(ctx.now, f"post(s) {', '.join(ids)} state {chosen[0].get('state')}", ids[0], "matching post",
                                      {"postIds": ids, "type": kind, "state": chosen[0].get("state")})


def register_postiz(registry, http: Http, *, capability: str = "content.publish", accept_unique_match: bool = False) -> PostizProbes:
    p = PostizProbes(http, accept_unique_match=accept_unique_match)
    registry.register(capability, p.publish, QUALIFICATION, ref_keys=(), ref_derive=derive_ref)
    return p
