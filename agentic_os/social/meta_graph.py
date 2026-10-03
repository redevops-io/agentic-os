"""Meta Graph publisher — post to a Facebook Page and publish to an Instagram Business account.

There is no official Meta "Muse" MCP: Meta Muse connects via Meta's own Connector system (and reads, not
publishes), so programmatic posting goes through the Graph API directly (or a scheduler like the bundled Postiz,
which this complements). This is the direct path for FB Page feed posts and IG media publishing.

  * Facebook Page feed:  POST /{page_id}/feed            {message, link?}            -> post id
  * Instagram publish:   POST /{ig_user_id}/media        {image_url, caption}        -> creation id
                         POST /{ig_user_id}/media_publish {creation_id}              -> media id

Credential-gated (a Page / IG access token) and offline-testable via an injected transport. IG requires a
public ``image_url`` (the Graph API fetches it); captions are optional.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

Transport = Callable[[str, str, Optional[dict], Optional[dict]], dict]

_GRAPH = "https://graph.facebook.com/v21.0"


def _http(method: str, url: str, token: str, payload: Optional[dict]) -> dict:
    import json
    import urllib.request
    data = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - fixed Graph host
        body = r.read().decode("utf-8")
        return json.loads(body) if body else {}


@dataclass(frozen=True)
class PublishResult:
    ok: bool
    platform: str                 # "facebook" | "instagram"
    post_id: str = ""
    error: str = ""


class MetaGraphPublisher:
    def __init__(self, access_token: str = "", *, transport: Transport = _http):
        self._token = access_token
        self._t = transport

    def entitled(self) -> bool:
        return bool(self._token)

    def publish_facebook(self, page_id: str, message: str, *, link: str = "") -> PublishResult:
        """Post to a Facebook Page's feed. Returns the created post id."""
        if not self._token:
            return PublishResult(False, "facebook", error="no access token")
        body: dict = {"message": message}
        if link:
            body["link"] = link
        resp = self._t("POST", f"{_GRAPH}/{page_id}/feed", self._token, body)
        if "id" in resp:
            return PublishResult(True, "facebook", post_id=resp["id"])
        return PublishResult(False, "facebook", error=str((resp.get("error", {}) or {}).get("message", resp)))

    def publish_instagram(self, ig_user_id: str, image_url: str, *, caption: str = "") -> PublishResult:
        """Two-step IG publish: create a media container, then publish it. Returns the published media id."""
        if not self._token:
            return PublishResult(False, "instagram", error="no access token")
        create = self._t("POST", f"{_GRAPH}/{ig_user_id}/media", self._token,
                         {"image_url": image_url, "caption": caption})
        creation_id = create.get("id")
        if not creation_id:
            return PublishResult(False, "instagram",
                                 error=str((create.get("error", {}) or {}).get("message", create)))
        pub = self._t("POST", f"{_GRAPH}/{ig_user_id}/media_publish", self._token,
                      {"creation_id": creation_id})
        if "id" in pub:
            return PublishResult(True, "instagram", post_id=pub["id"])
        return PublishResult(False, "instagram", error=str((pub.get("error", {}) or {}).get("message", pub)))
