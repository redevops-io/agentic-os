"""Generation + publishing adapters for content missions.

Two seams, both behind Protocols so the mission is testable offline with fakes and runs live with real
HTTP clients without changing the mission:

  * Generator — produces the concept, the short-form video (vibexgen.io /api/reels), and per-channel
    copy (vibexgen.io /api/chat). Render-only: media is generated for PREVIEW; nothing is posted.
  * Publisher — posts an APPROVED draft to a channel. IG/TikTok go through vibexgen's /api/publish; X and
    LinkedIn go through their own APIs. A channel with no configured publisher degrades to a MANUAL
    HANDOFF (the approved copy + media are returned for the owner to post), never a silent drop.

All live clients read credentials from the environment and never log them; a missing credential yields a
recorded error / manual handoff, not a crash. Publishing only ever happens after the mission's approval
gate — these adapters do not gate; the mission does.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Optional, Protocol

from .contracts import Channel, ChannelDraft, ContentBrief, ContentConcept, PublishOutcome


# ──────────────────────────── protocols ────────────────────────────

class Generator(Protocol):
    def generate_concept(self, brief: ContentBrief) -> ContentConcept: ...
    def render_reel(self, brief: ContentBrief, concept: ContentConcept) -> dict: ...   # {video_url, preview_url, task_id}
    def draft_text(self, brief: ContentBrief, concept: ContentConcept, channel: Channel) -> str: ...
    def generate_image(self, brief: ContentBrief, concept: ContentConcept) -> dict: ...  # {image_url}; may be {}


class Publisher(Protocol):
    def can_publish(self, channel: Channel) -> bool: ...
    def publish(self, draft: ChannelDraft) -> ChannelDraft: ...


# ──────────────────────────── fakes (offline runs + tests) ────────────────────────────

class FakeGenerator:
    """Deterministic generator for offline mission runs and tests — no network, clearly synthetic."""

    def generate_concept(self, brief: ContentBrief) -> ContentConcept:
        return ContentConcept(
            hook=f"How {brief.subject} stopped missing local contracts",
            narrative=brief.key_message,
            key_points=(brief.angle, brief.cta or "See how it works"))

    def render_reel(self, brief: ContentBrief, concept: ContentConcept) -> dict:
        return {"video_url": f"https://fixture.vibexgen/reels/{brief.campaign_id}.mp4",
                "preview_url": f"https://fixture.vibexgen/previews/{brief.campaign_id}.mp4",
                "task_id": f"reel-{brief.campaign_id}"}

    def generate_image(self, brief: ContentBrief, concept: ContentConcept) -> dict:
        return {"image_url": f"https://fixture.vibexgen/images/{brief.campaign_id}.png"}

    def draft_text(self, brief: ContentBrief, concept: ContentConcept, channel: Channel) -> str:
        return f"[{channel.value}] {concept.hook}. {concept.narrative} {brief.cta}".strip()


class FakePublisher:
    """Records publishes for tests; ``publishable`` decides which channels it 'can' post (others → manual)."""

    def __init__(self, publishable: tuple[Channel, ...] = ()):
        self.publishable = set(publishable)
        self.sent: list[ChannelDraft] = []

    def can_publish(self, channel: Channel) -> bool:
        return channel in self.publishable

    def publish(self, draft: ChannelDraft) -> ChannelDraft:
        self.sent.append(draft)
        draft.status = PublishOutcome.PUBLISHED.value
        draft.post_id = f"fake-{draft.channel.value}-1"
        draft.post_url = f"https://fixture.social/{draft.channel.value}/1"
        return draft


# ──────────────────────────── vibexgen.io generation ────────────────────────────

def _http_json(url: str, *, method: str = "GET", headers: dict, body: Optional[dict] = None,
               timeout: float = 30.0) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (explicit opt-in)
            txt = r.read().decode("utf-8", "ignore")
            try:
                return getattr(r, "status", 200), json.loads(txt)
            except (json.JSONDecodeError, ValueError):
                return getattr(r, "status", 200), {"raw": txt}
    except urllib.error.HTTPError as e:
        # an HTTP error carries the provider's real message in its body (X's "detail"/"title", the
        # LinkedIn error). Surface it instead of a bare "<HTTPError 403>" so a failed receipt is diagnosable.
        try:
            payload = json.loads(e.read().decode("utf-8", "ignore"))
        except Exception:  # noqa: BLE001
            payload = {}
        msg = (payload.get("detail") or payload.get("title") or payload.get("message")
               or payload.get("error") or f"HTTP {e.code}")
        if payload.get("detail") and payload.get("title") and payload["title"] not in msg:
            msg = f"{payload['title']}: {payload['detail']}"
        return e.code, {"error": msg, **({"body": payload} if payload else {})}
    except Exception as e:  # noqa: BLE001
        return 0, {"error": repr(e)}


class VibexgenGenerator:
    """Generation via the vibexgen.io gateway (sidekick-reel): concept + copy via /api/chat, short video
    via /api/reels (render-only, publish_targets=[]). Auth: a `vxg_` bearer key + the service token."""

    def __init__(self, base_url: str, api_key: str, service_token: str = "",
                 *, provider: str = "seedance", aspect: str = "9:16",
                 poll_timeout_s: float = 600.0, poll_interval_s: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self._key = api_key
        self._service_token = service_token
        self.provider = provider
        self.aspect = aspect
        self.poll_timeout_s = poll_timeout_s
        self.poll_interval_s = poll_interval_s

    @classmethod
    def from_env(cls, prefix: str = "VIBEXGEN", **kw) -> "VibexgenGenerator":
        return cls(os.environ.get(f"{prefix}_BASE_URL", "https://vibexgen.io"),
                   os.environ.get(f"{prefix}_API_KEY", ""),
                   os.environ.get("REEL_SERVICE_TOKEN", ""), **kw)

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", "Authorization": f"Bearer {self._key}"}
        if self._service_token:
            h["X-Reel-Service-Token"] = self._service_token
        return h

    def _chat(self, prompt: str) -> str:
        status, data = _http_json(f"{self.base_url}/api/chat", method="POST",
                                  headers=self._headers(), body={"message": prompt})
        return (data.get("reply") or "").strip() if status == 200 else ""

    def generate_concept(self, brief: ContentBrief) -> ContentConcept:
        reply = self._chat(
            f"You are a social copywriter. Angle: {brief.angle}. Subject: {brief.subject}. "
            f"Key message: {brief.key_message}. Give a one-line hook, then a 2-sentence narrative.")
        hook, _, narrative = reply.partition("\n")
        return ContentConcept(hook=hook.strip() or brief.key_message,
                              narrative=narrative.strip() or brief.key_message,
                              key_points=(brief.angle,))

    def render_reel(self, brief: ContentBrief, concept: ContentConcept) -> dict:
        status, data = _http_json(
            f"{self.base_url}/api/reels", method="POST", headers=self._headers(),
            body={"brief": f"{concept.hook}. {concept.narrative}"[:2000], "provider": self.provider,
                  "aspect": self.aspect, "publish_targets": []})   # render-only for preview
        if status not in (200, 202) or not data.get("task_id"):
            return {"video_url": "", "task_id": "", "error": data.get("error", f"status {status}")}
        task_id = data["task_id"]
        deadline = time.monotonic() + self.poll_timeout_s
        while time.monotonic() < deadline:
            s, d = _http_json(f"{self.base_url}/api/reels/{task_id}", headers=self._headers())
            state = (d.get("status") or "").lower()
            if state in ("done", "success", "succeeded", "completed"):
                result = d.get("result") or {}
                return {"video_url": result.get("final_video_url") or result.get("final_url", ""),
                        "preview_url": result.get("preview_url", ""),   # presigned, externally viewable
                        "task_id": task_id}
            if state in ("error", "failed"):
                return {"video_url": "", "task_id": task_id, "error": d.get("error", "render failed")}
            time.sleep(self.poll_interval_s)
        return {"video_url": "", "task_id": task_id, "error": "render timed out"}

    def generate_image(self, brief: ContentBrief, concept: ContentConcept) -> dict:
        """Generate a hero/thumbnail image via a paid fal.ai t2i model. Best-effort: returns {} if the
        gateway has no image endpoint yet (a follow-up gateway addition), so callers degrade gracefully."""
        status, data = _http_json(
            f"{self.base_url}/api/image", method="POST", headers=self._headers(),
            body={"prompt": f"{concept.hook} — {brief.subject}, clean modern brand visual, no text",
                  "provider": "flux", "aspect": "1:1"})
        if status in (200, 202):
            return {"image_url": data.get("image_url") or data.get("url") or data.get("preview_url", "")}
        return {}

    def draft_text(self, brief: ContentBrief, concept: ContentConcept, channel: Channel) -> str:
        style = {"x": "a punchy X/Twitter post under 280 chars",
                 "linkedin": "a professional LinkedIn post, 3 short paragraphs"}.get(
                     channel.value, "a short social caption")
        reply = self._chat(f"Write {style} for {brief.subject}. Hook: {concept.hook}. "
                           f"Message: {concept.narrative}. CTA: {brief.cta}")
        return reply or f"{concept.hook} {brief.cta}".strip()


# ──────────────────────────── publishers ────────────────────────────

class VibexgenPublisher:
    """Publishes video channels (Instagram/TikTok) through vibexgen's /api/publish."""

    _SUPPORTED = {Channel.INSTAGRAM, Channel.TIKTOK}

    def __init__(self, base_url: str, api_key: str, service_token: str = ""):
        self.base_url = base_url.rstrip("/")
        self._key = api_key
        self._service_token = service_token

    @classmethod
    def from_env(cls, prefix: str = "VIBEXGEN") -> "VibexgenPublisher":
        return cls(os.environ.get(f"{prefix}_BASE_URL", "https://vibexgen.io"),
                   os.environ.get(f"{prefix}_API_KEY", ""), os.environ.get("REEL_SERVICE_TOKEN", ""))

    def can_publish(self, channel: Channel) -> bool:
        return channel in self._SUPPORTED and bool(self._key)

    def publish(self, draft: ChannelDraft) -> ChannelDraft:
        if not draft.media_url:
            draft.status, draft.error = PublishOutcome.FAILED.value, "no media_url to publish"
            return draft
        h = {"Content-Type": "application/json", "Authorization": f"Bearer {self._key}"}
        if self._service_token:
            h["X-Reel-Service-Token"] = self._service_token
        status, data = _http_json(f"{self.base_url}/api/publish", method="POST", headers=h,
                                  body={"video_url": draft.media_url, "caption": draft.text,
                                        "targets": [draft.channel.value]})
        if status == 200:
            draft.status = PublishOutcome.PUBLISHED.value
            draft.post_id = str(data.get("instagram_post_id") or data.get("tiktok_publish_id") or "")
            draft.post_url = data.get("post_url", "")
        else:
            draft.status, draft.error = PublishOutcome.FAILED.value, data.get("error", f"status {status}")
        return draft


def _oauth1_header(method: str, url: str, ck: str, cs: str, at: str, ats: str,
                   *, nonce: Optional[str] = None, ts: Optional[str] = None) -> str:
    """Build an OAuth 1.0a HMAC-SHA1 Authorization header. For a JSON body (X API v2 POST /2/tweets) the
    body params are NOT part of the signature base — only the oauth_* params are — which is what X expects."""
    import base64
    import hashlib
    import hmac
    import secrets as _secrets
    import time as _time
    from urllib.parse import quote
    enc = lambda s: quote(str(s), safe="~")
    oauth = {
        "oauth_consumer_key": ck, "oauth_nonce": nonce or _secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1", "oauth_timestamp": ts or str(int(_time.time())),
        "oauth_token": at, "oauth_version": "1.0",
    }
    param_str = "&".join(f"{enc(k)}={enc(v)}" for k, v in sorted(oauth.items()))
    base = "&".join([method.upper(), enc(url), enc(param_str)])
    signing_key = f"{enc(cs)}&{enc(ats)}"
    oauth["oauth_signature"] = base64.b64encode(
        hmac.new(signing_key.encode(), base.encode(), hashlib.sha1).digest()).decode()
    return "OAuth " + ", ".join(f'{enc(k)}="{enc(v)}"' for k, v in sorted(oauth.items()))


class XPublisher:
    """Posts to X/Twitter via API v2 (POST /2/tweets), signed with OAuth 1.0a user context (consumer
    key/secret + access token/secret)."""

    def __init__(self, consumer_key: str, consumer_secret: str, access_token: str, access_secret: str):
        self._ck, self._cs, self._at, self._ats = consumer_key, consumer_secret, access_token, access_secret

    @classmethod
    def from_env(cls, prefix: str = "X") -> "XPublisher":
        import re as _re
        ck = os.environ.get(f"{prefix}_CONSUMER_KEY", "")
        cs = os.environ.get(f"{prefix}_CONSUMER_SECRET", "")
        # canonical, unambiguous pair (matches the exported creds): access TOKEN + access SECRET.
        at = os.environ.get(f"{prefix}_ACCESS_TOKEN", "")
        ats = os.environ.get(f"{prefix}_ACCESS_SECRET", "")
        if at and ats:
            return cls(ck, cs, at, ats)
        # legacy/ambiguous names X_ACCESS_KEY / X_ACCESS_TOKEN: the real access TOKEN starts with
        # "<digits>-", the access SECRET does not — detect rather than guess the mapping.
        a, b = os.environ.get(f"{prefix}_ACCESS_KEY", ""), at
        if _re.match(r"^\d+-", a):
            at, ats = a, b
        elif _re.match(r"^\d+-", b):
            at, ats = b, a
        else:
            at, ats = a, b        # fallback: assume ACCESS_KEY=token, ACCESS_TOKEN=secret
        return cls(ck, cs, at, ats)

    def can_publish(self, channel: Channel) -> bool:
        return channel is Channel.X and all((self._ck, self._cs, self._at, self._ats))

    def publish(self, draft: ChannelDraft) -> ChannelDraft:
        url = "https://api.x.com/2/tweets"
        h = {"Content-Type": "application/json",
             "Authorization": _oauth1_header("POST", url, self._ck, self._cs, self._at, self._ats)}
        status, data = _http_json(url, method="POST", headers=h, body={"text": draft.text[:280]})
        tid = (data.get("data") or {}).get("id") if isinstance(data.get("data"), dict) else None
        if status in (200, 201) and tid:
            draft.status, draft.post_id = PublishOutcome.PUBLISHED.value, str(tid)
            draft.post_url = f"https://x.com/i/web/status/{tid}"
        else:
            draft.status, draft.error = PublishOutcome.FAILED.value, data.get("error", f"status {status}")
        return draft


class LinkedInPublisher:
    """Posts to LinkedIn via the UGC Posts API. Access token + author URN from env."""

    def __init__(self, access_token: str, author_urn: str):
        self._token = access_token
        self._author = author_urn

    @classmethod
    def from_env(cls, prefix: str = "LINKEDIN") -> "LinkedInPublisher":
        return cls(os.environ.get(f"{prefix}_ACCESS_TOKEN", ""), os.environ.get(f"{prefix}_AUTHOR_URN", ""))

    def can_publish(self, channel: Channel) -> bool:
        return channel is Channel.LINKEDIN and bool(self._token and self._author)

    def publish(self, draft: ChannelDraft) -> ChannelDraft:
        h = {"Content-Type": "application/json", "Authorization": f"Bearer {self._token}",
             "X-Restli-Protocol-Version": "2.0.0"}
        body = {"author": self._author, "lifecycleState": "PUBLISHED",
                "specificContent": {"com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": draft.text},
                    "shareMediaCategory": "NONE"}},
                "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"}}
        status, data = _http_json("https://api.linkedin.com/v2/ugcPosts", method="POST", headers=h, body=body)
        pid = data.get("id")
        if status in (200, 201) and pid:
            draft.status, draft.post_id = PublishOutcome.PUBLISHED.value, str(pid)
            draft.post_url = f"https://www.linkedin.com/feed/update/{pid}"
        else:
            draft.status, draft.error = PublishOutcome.FAILED.value, data.get("error", f"status {status}")
        return draft


class NoOpPublisher:
    """Publishes nothing — used when a higher layer (the Projects content service) owns the SELECTIVE
    publish + receipts, so the mission's internal publish gate can resolve for governance/timeline without
    double-posting to any channel."""

    def can_publish(self, channel: Channel) -> bool:
        return True

    def publish(self, draft: ChannelDraft) -> ChannelDraft:
        draft.status = PublishOutcome.MANUAL_HANDOFF.value
        draft.error = "publish owned by Projects content service (no-op here)"
        return draft


class MultiPublisher:
    """Routes each channel to the first configured publisher that can post it; a channel with no publisher
    degrades to a MANUAL HANDOFF (approved copy + media returned for the owner to post) — never dropped."""

    def __init__(self, publishers: list):
        self.publishers = publishers

    def can_publish(self, channel: Channel) -> bool:
        return any(p.can_publish(channel) for p in self.publishers)

    def publish(self, draft: ChannelDraft) -> ChannelDraft:
        for p in self.publishers:
            if p.can_publish(draft.channel):
                return p.publish(draft)
        draft.status = PublishOutcome.MANUAL_HANDOFF.value
        draft.error = f"no publisher configured for {draft.channel.value}; approved copy handed off"
        return draft

    @classmethod
    def from_env(cls) -> "MultiPublisher":
        """All live publishers, each reading its own env creds; unconfigured ones simply can't publish
        (→ manual handoff for their channels)."""
        return cls([VibexgenPublisher.from_env(), XPublisher.from_env(), LinkedInPublisher.from_env()])
