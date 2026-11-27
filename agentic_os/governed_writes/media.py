"""Media and social publishing as governed work: the VibeXGen operator makes and edits files, Postiz drafts and schedules
posts, and this holds what neither of them knows: which tenant each file belongs to, and exactly which bytes a person
approved for which channel.

* **Ownership.** VibeXGen keeps one asset store with no tenants, so the ledger here (``media.sqlite``) records every file a
  tenant's agent made. A file the tenant did not make does not exist for it.
* **Only exported files are posted.** The final export is where the platform's size is checked and where a file that a model
  had any part in gets its "AI-generated" label, from its lineage. A post therefore takes only a file ``media.export``
  made for a platform that matches the channel.
* **The approval names the bytes.** The card shows the file's SHA-256, size, target, source footage and AI label; those are
  part of the digest approved. At posting time the file is fetched again and must hash to the same value.
* **Never "now".** A post is saved as a draft or scheduled for at least ``MIN_LEAD_S`` ahead, so a person can still stop it
  in Postiz. There is no publish-immediately action.
* **Generation** spends money or GPU time with a model, so it is its own approved action; VibeXGen's runtime verifies a
  single-use approval token bound to the exact inputs.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from agentic_os.governed_writes.apps import NotApplied, OutcomeUnknown, _Http

VIBEXGEN_OPERATOR = "vibexgen"
MAX_MEDIA_BYTES = 256 * 1024 * 1024
MIN_LEAD_S = 15 * 60
MAX_AHEAD_S = 366 * 86400
#: an export target -> the Postiz channel kinds it may be posted to
TARGET_CHANNELS: Dict[str, tuple] = {
    "instagram_reel": ("instagram",), "instagram_post": ("instagram",), "youtube_short": ("youtube",), "tiktok": ("tiktok",),
    "linkedin_video": ("linkedin",), "linkedin_post": ("linkedin",),
    "square_video": ("instagram", "facebook", "linkedin", "x"), "square_image": ("instagram", "facebook", "linkedin", "x"),
}
ATTEST_NONE = ""


# ── how a file is named outside ──────────────────────────────────────────────────
#: VibeXGen's asset ids are 32 hex digits, which the gateway's egress rules rightly treat as a secret. Agents are given the
#: same id written as a UUID (a record id, which they may see); the egress rules are unchanged.
PUBLIC_ASSET = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


def public_id(asset: str) -> str:
    a = str(asset)
    return f"{a[:8]}-{a[8:12]}-{a[12:16]}-{a[16:20]}-{a[20:]}" if len(a) == 32 else a


def asset_id(public: str) -> str:
    if not re.match(PUBLIC_ASSET, str(public)):
        raise ValueError("an asset id is written as media tools give it (8-4-4-4-12)")
    return str(public).replace("-", "")


# ── the applications ─────────────────────────────────────────────────────────────
@dataclass
class VibexgenOperator(_Http):
    """VibeXGen's operator app (``vibexgen.rails_app``): ``/invoke`` with its control token, finished files with the
    download token."""
    base_url: str
    control_token: str
    download_token: str = ""
    timeout: float = 600.0                       # a render is slow
    transport: Any = None

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.control_token}", "Content-Type": "application/json"}

    def health(self) -> Dict[str, Any]:
        return self._call("GET", "/health")

    def invoke(self, capability: str, inputs: Mapping[str, Any], key: str, *, side_effecting: bool = False, **extra: Any) -> Dict[str, Any]:
        """The operator's result. A transport failure of a side-effecting call is ``OutcomeUnknown``; the runtime dedupes
        by ``key``, so the caller decides whether asking again is safe."""
        body = {"capability": capability, "inputs": dict(inputs), "idempotency_key": key, **extra}
        try:
            out = self._call("POST", "/invoke", body=body)
        except OutcomeUnknown as e:
            if side_effecting:
                raise
            raise NotApplied(str(e)) from None
        result = out.get("result") if isinstance(out, dict) else None
        if not isinstance(result, dict):
            raise (OutcomeUnknown if side_effecting else NotApplied)("the media service's answer had no result")
        return result

    def download(self, asset: str, limit: int = MAX_MEDIA_BYTES) -> bytes:
        import httpx  # noqa: PLC0415
        if not self.download_token:
            raise NotApplied("this deployment holds no download token for the media service (VIBEXGEN_DOWNLOAD_TOKEN)")
        try:
            with httpx.Client(timeout=self.timeout, transport=self.transport) as client:
                with client.stream("GET", f"{self.base_url}/assets/{asset}/download",
                                   headers={"Authorization": f"Bearer {self.download_token}"}) as r:
                    if r.status_code != 200:
                        raise NotApplied(f"the media service would not give the file (HTTP {r.status_code})")
                    data = bytearray()
                    for chunk in r.iter_bytes():
                        data += chunk
                        if len(data) > limit:
                            raise NotApplied("the file is too large to post")
        except NotApplied:
            raise
        except Exception as e:  # noqa: BLE001
            raise NotApplied(f"the file could not be fetched ({type(e).__name__})") from None
        return bytes(data)


@dataclass
class PostizClient(_Http):
    """Postiz's public API (``.../public/v1``) with the organisation's API key."""
    base_url: str
    api_key: str
    timeout: float = 120.0
    transport: Any = None

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": self.api_key}

    def channels(self) -> List[Dict[str, Any]]:
        rows = self._call("GET", "/integrations")
        if not isinstance(rows, list):
            raise NotApplied("the list of channels could not be read")
        return [{"id": str(r.get("id", "")), "name": str(r.get("name", "")), "kind": str(r.get("identifier", "")),
                 "disabled": bool(r.get("disabled"))} for r in rows]

    def posts_in_month(self, when: _dt.datetime) -> List[Dict[str, Any]]:
        params = {"display": "month", "month": str(when.month), "year": str(when.year),
                  "week": str(min(52, when.isocalendar()[1])), "day": str(when.weekday() + 1 if when.weekday() < 6 else 0)}
        out = self._call("GET", "/posts", params=params)
        rows = out.get("posts") if isinstance(out, dict) else None
        if not isinstance(rows, list):
            raise NotApplied("the calendar could not be read")
        return rows

    def upload(self, data: bytes, filename: str, media_type: str) -> Dict[str, str]:
        import httpx  # noqa: PLC0415
        try:
            with httpx.Client(timeout=self.timeout, transport=self.transport) as client:
                r = client.post(f"{self.base_url}/upload", headers=self._headers(), files={"file": (filename, data, media_type)})
        except Exception as e:  # noqa: BLE001 - nothing was posted: an uploaded file on its own publishes nothing
            raise NotApplied(f"the file could not be uploaded ({type(e).__name__})") from None
        try:
            out = r.json() if 200 <= r.status_code < 300 else {}
        except ValueError:
            out = {}
        if not out.get("id") or not out.get("path"):
            raise NotApplied(f"the upload was not accepted (HTTP {r.status_code})")
        return {"id": str(out["id"]), "path": str(out["path"])}

    def create_post(self, *, kind: str, when: str, channel: str, caption: str, media: Optional[Dict[str, str]]) -> str:
        body = {"type": kind, "date": when, "shortLink": False,
                "posts": [{"integration": {"id": channel}, "settings": {"__type": "fallback"},
                           "value": [{"content": caption, "image": [media] if media else []}]}]}
        out = self._call("POST", "/posts", body=body)
        made = out[0] if isinstance(out, list) and out else {}
        if not made.get("postId"):
            raise OutcomeUnknown("Postiz did not say which post it created")
        return str(made["postId"])


def operators_from_env() -> Dict[str, Any]:
    env = lambda k: os.environ.get(k, "").strip()  # noqa: E731
    out: Dict[str, Any] = {}
    if env("VIBEXGEN_OPERATOR_URL") and env("VIBEXGEN_CONTROL_TOKEN"):
        out["vibexgen"] = VibexgenOperator(env("VIBEXGEN_OPERATOR_URL").rstrip("/"), env("VIBEXGEN_CONTROL_TOKEN"), env("VIBEXGEN_DOWNLOAD_TOKEN"))
    if env("POSTIZ_PUBLIC_API_URL") and env("POSTIZ_API_KEY"):
        out["postiz"] = PostizClient(env("POSTIZ_PUBLIC_API_URL").rstrip("/"), env("POSTIZ_API_KEY"))
    return out


# ── whose file is what ───────────────────────────────────────────────────────────
class Ledger:
    def __init__(self, state_dir: "str | Path", clock: Callable[[], float] = time.time) -> None:
        db = Path(state_dir) / "media.sqlite"
        if not db.exists():
            os.close(os.open(db, os.O_CREAT | os.O_WRONLY, 0o600))
        self.clock, self._lock = clock, threading.RLock()
        self._db = sqlite3.connect(str(db), check_same_thread=False, isolation_level=None, timeout=30)
        self._db.execute("CREATE TABLE IF NOT EXISTS assets(tenant TEXT NOT NULL, asset TEXT NOT NULL, data TEXT NOT NULL, created REAL NOT NULL,"
                         " PRIMARY KEY(tenant, asset))")

    def record(self, tenant: str, entry: Mapping[str, Any]) -> None:
        with self._lock:                         # the first record of a file stands: nothing rewrites how it was made
            self._db.execute("INSERT OR IGNORE INTO assets VALUES(?,?,?,?)", (tenant, str(entry["asset"]), json.dumps(dict(entry)), self.clock()))

    def get(self, tenant: str, asset: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._db.execute("SELECT data FROM assets WHERE tenant=? AND asset=?", (tenant, str(asset))).fetchone()
        return json.loads(row[0]) if row else None

    def list(self, tenant: str, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT data FROM assets WHERE tenant=? ORDER BY created DESC LIMIT ?", (tenant, limit)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def sources(self, tenant: str, asset: str) -> List[str]:
        """The library items (and generated files) this file was made from."""
        seen, todo, out = set(), [asset], []
        while todo:
            a = todo.pop()
            entry = None if a in seen else self.get(tenant, a)
            seen.add(a)
            if entry is None:
                continue
            if entry.get("library_item"):
                out.append(f"library item {entry['library_item']}")
            if entry.get("how") == "generate":
                out.append(f"generated {a}")
            todo += entry.get("parents", [])
        return sorted(set(out))


def _ledger(ctx: Any) -> Ledger:
    return Ledger(ctx.state_dir, ctx.journal.clock)


def _own(public: Any) -> str:
    try:
        return asset_id(public)
    except ValueError as e:
        raise NotApplied(str(e)) from None


# ── drafting and scheduling a post ───────────────────────────────────────────────
def parse_when(value: str, now: float) -> Optional[_dt.datetime]:
    """``draft`` -> None; otherwise a UTC time far enough ahead that a person can still stop the post."""
    if value == "draft":
        return None
    try:
        at = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise NotApplied("when must be 'draft' or an ISO 8601 time such as 2027-01-31T15:00:00Z") from None
    if at.tzinfo is None:
        raise NotApplied("the time needs a time zone (end it with Z for UTC)")
    if at.timestamp() < now + MIN_LEAD_S:
        raise NotApplied(f"a post can only be scheduled at least {MIN_LEAD_S // 60} minutes ahead: publishing now is not offered")
    if at.timestamp() > now + MAX_AHEAD_S:
        raise NotApplied("a post can be scheduled at most a year ahead")
    return at.astimezone(_dt.timezone.utc)


def _channel(postiz: Any, channel_id: str) -> Dict[str, Any]:
    found = next((c for c in postiz.channels() if c["id"] == channel_id), None)
    if found is None:
        raise NotApplied("there is no such channel: take its id from social.channels")
    if found["disabled"]:
        raise NotApplied(f"the channel {found['name']} is disabled")
    return found


def _postable(ledger: Ledger, tenant: str, asset: str, channel: Mapping[str, Any]) -> Dict[str, Any]:
    entry = ledger.get(tenant, asset)
    if entry is None:
        raise NotApplied("there is no such media file of yours")
    if entry.get("how") != "export" or entry.get("target") not in TARGET_CHANNELS:
        raise NotApplied("only a file made by media.export can be posted: export it for the channel's platform first")
    kind = str(channel["kind"]).split("-", 1)[0].lower()
    if kind not in TARGET_CHANNELS[entry["target"]]:
        raise NotApplied(f"this file was exported for {entry['target']}, which is not for a {kind} channel")
    if int(entry.get("bytes") or 0) > MAX_MEDIA_BYTES:
        raise NotApplied("the file is too large to post")
    return entry


def prepare_post(ctx: Any, postiz: Any, i: Mapping[str, Any]) -> Dict[str, Any]:
    at = parse_when(str(i["when"]), ctx.journal.clock())
    channel = _channel(postiz, str(i["channel_id"]))
    out: Dict[str, Any] = {"channel": channel, "scheduled_for": at.isoformat() if at else None}
    show = [["Channel", f"{channel['name']} ({channel['kind']})"],
            ["What happens", f"scheduled for {at:%Y-%m-%d %H:%M UTC}; it can be stopped in Postiz until then" if at else
             "saved as a draft in Postiz: nothing is published"]]
    if i.get("asset"):
        ledger = _ledger(ctx)
        entry = _postable(ledger, ctx.tenant, _own(i["asset"]), channel)
        sources = ledger.sources(ctx.tenant, entry["asset"])
        out["file"] = {k: entry.get(k) for k in ("asset", "sha256", "bytes", "media_type", "target", "ai_generated", "ai_label", "duration")}
        out["file"]["sources"] = sources
        show += [["Attached file", f"{entry['media_type']}, {entry['bytes']} bytes, exported for {entry['target']}"
                                   + (f", {entry['duration']} s" if entry.get("duration") else "")],
                 ["File SHA-256", entry["sha256"]], ["Made from", ", ".join(sources) or "not recorded"],
                 ["AI-generated", "yes: the export burned the label into the picture" if entry.get("ai_generated") else "no model had a part in it"]]
    what = f"Schedule a post on {channel['name']} for {at:%Y-%m-%d %H:%M UTC}" if at else f"Save a draft post for {channel['name']}"
    return {**out, "show": show, "summary": what}


def perform_post(postiz: Any, i: Mapping[str, Any], footer: str, rid: str, ctx: Any):
    from agentic_os.governed_writes.actions import Outcome  # noqa: PLC0415
    approved = dict(ctx.evidence or {})
    at = parse_when(str(i["when"]), ctx.journal.clock())                  # still far enough ahead when it is performed
    channel = _channel(postiz, str(i["channel_id"]))
    if channel != approved.get("channel"):
        raise NotApplied("the channel changed since it was approved: propose it again")
    media = None
    if i.get("asset"):
        entry = _postable(_ledger(ctx), ctx.tenant, _own(i["asset"]), channel)
        file = approved.get("file") or {}
        source = ctx.operators.get("vibexgen")
        if source is None:
            raise NotApplied("the media service is not connected")
        data = source.download(entry["asset"])
        if hashlib.sha256(data).hexdigest() != file.get("sha256"):
            raise NotApplied("the file is not the one that was approved (its SHA-256 differs)")
        suffix = {"video/mp4": ".mp4", "image/png": ".png", "image/jpeg": ".jpg", "video/quicktime": ".mov", "video/webm": ".webm"}
        media = postiz.upload(data, f"{entry['asset']}{suffix.get(entry['media_type'], '')}", entry["media_type"])
    kind = "schedule" if at else "draft"
    when = (at or _dt.datetime.fromtimestamp(ctx.journal.clock(), _dt.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    post = postiz.create_post(kind=kind, when=when, channel=channel["id"], caption=str(i["caption"]), media=media)
    wanted = "QUEUE" if at else "DRAFT"
    try:
        moment = at or _dt.datetime.fromtimestamp(ctx.journal.clock(), _dt.timezone.utc)
        mine = next((p for p in postiz.posts_in_month(moment) if str(p.get("id")) == post), None)
    except Exception as e:  # noqa: BLE001
        return Outcome(True, False, post, f"created, but the calendar could not be read back ({type(e).__name__})")
    if mine is None:
        return Outcome(True, False, post, "created, but it is not in the calendar: check Postiz")
    problems = []
    if str(mine.get("state")) != wanted:
        problems.append(f"its state is {mine.get('state')}, not {wanted}")
    if str((mine.get("integration") or {}).get("id")) != channel["id"]:
        problems.append("it is on another channel")
    if str(i["caption"]) not in str(mine.get("content") or ""):
        problems.append("its text differs from what was approved")
    if at and str(mine.get("publishDate") or "")[:16] != when[:16]:
        problems.append(f"its time is {mine.get('publishDate')}, not {when}")
    if problems:
        return Outcome(True, False, post, "created, but read back differently: " + "; ".join(problems))
    return Outcome(True, True, post, (f"scheduled for {when} on {channel['name']}" if at else f"a draft on {channel['name']}")
                   + ", read back from Postiz" + (" with the approved file attached" if media else ""))


# ── generating with a model ──────────────────────────────────────────────────────
def _generate_inputs(ctx: Any, i: Mapping[str, Any]) -> Dict[str, Any]:
    out = {k: i[k] for k in ("kind", "prompt", "source_asset", "width", "height", "duration", "seed") if i.get(k) is not None}
    if out.get("source_asset"):
        out["source_asset"] = _own(out["source_asset"])
        if _ledger(ctx).get(ctx.tenant, out["source_asset"]) is None:
            raise NotApplied("there is no such media file of yours")
    return out


def prepare_generate(ctx: Any, operator: Any, i: Mapping[str, Any]) -> Dict[str, Any]:
    _generate_inputs(ctx, i)
    health = operator.health()
    backend = health.get(f"{i['kind']}_backend") if isinstance(health, dict) else None
    if not backend:
        raise NotApplied(f"no {i['kind']} generation model is configured on this deployment")
    notices = [str(n) for n in (health.get("notices") or [])][:5]
    show = [["Model service", str(backend)], *([["Notices", "; ".join(notices)]] if notices else [])]
    return {"backend": str(backend), "show": show, "summary": f"Generate a {i['kind']} with {backend}"}


def perform_generate(operator: Any, i: Mapping[str, Any], footer: str, rid: str, ctx: Any):
    from agentic_os.governed_writes.actions import Outcome  # noqa: PLC0415
    from agentic_os.mission.approval import mint_approval_token  # noqa: PLC0415
    inputs = _generate_inputs(ctx, i)
    health = operator.health()
    if (health.get(f"{i['kind']}_backend") or "") != (ctx.evidence or {}).get("backend"):
        raise NotApplied("the model service changed since it was approved: propose it again")
    marker = {"approved": True, "by": ctx.approver, "tenant": ctx.tenant}
    token = mint_approval_token(None, operator=VIBEXGEN_OPERATOR, capability="visual.generate", mission_id=rid, node_id="generate",
                                inputs=inputs, marker=marker)
    if not token:
        raise NotApplied("no approval secret is shared with the media service (AGENTIC_OS_APPROVAL_SECRET)")
    result = operator.invoke("visual.generate", inputs, f"generate:{rid}", side_effecting=True, approval_token=token, mission_id=rid, node_id="generate")
    arts = result.get("artifacts") or []
    if result.get("status") != "ok" or not arts:
        why = str(result.get("error") or result.get("status") or "no file")[:300]
        raise NotApplied(f"the media service did not generate it: {why}")
    ref = arts[0]
    _ledger(ctx).record(ctx.tenant, {**ref, "how": "generate", "by": ctx.requester, "ai_generated": True,
                                     "parents": [inputs["source_asset"]] if inputs.get("source_asset") else []})
    return Outcome(True, True, public_id(ref["asset"]), f"generated {ref.get('media_type')}, {ref.get('bytes')} bytes")


__all__ = ["PUBLIC_ASSET", "public_id", "asset_id", "VibexgenOperator", "PostizClient", "Ledger", "operators_from_env", "parse_when", "prepare_post", "perform_post",
           "prepare_generate", "perform_generate", "TARGET_CHANNELS", "MIN_LEAD_S", "MAX_MEDIA_BYTES", "VIBEXGEN_OPERATOR"]
