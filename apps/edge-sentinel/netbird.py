"""edge-sentinel · NetBird zero-trust network access plane (the "who can reach what" half).

CrowdSec is the runtime/perimeter half of security; NetBird is the ACCESS half — a WireGuard
overlay with central policies, SSO/MFA and device posture. This module reads NetBird's REST API
(Authorization: Token) to review peers + policies, flag over-broad access, and (approval-gated)
propose a tightened policy — mirroring block_ip→approve_block.

Self-contained: no context-runtime / agentic-os deps. Until NETBIRD_API_TOKEN is set the review
shows clearly-labelled SAMPLE data so the panel/tool always render; once set it reads the real
peers and policies. Page chrome (CSS/`_panel_shell`/`_tbl`/…) is reused from app.py via a
deferred import so there is no import cycle (app.py imports this module at load time).

Config (env): NETBIRD_API_URL / NETBIRD_API_TOKEN / NETBIRD_FRONT_URL.
"""
from __future__ import annotations

import os
from pathlib import Path

import httpx

# Idempotent .env load so the module is self-sufficient when imported standalone (importlib /
# the Mission Runtime operator) without app.py having run its own loader first — mirrors core.py.
_ENV_FILE = Path(__file__).resolve().parent / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

NETBIRD_API_URL = os.environ.get("NETBIRD_API_URL", "https://api.netbird.io/api").rstrip("/")
NETBIRD_API_TOKEN = os.environ.get("NETBIRD_API_TOKEN", "")
NETBIRD_FRONT_URL = os.environ.get("NETBIRD_FRONT_URL", "https://app.netbird.io").rstrip("/")


def _nb_get(path: str):
    """GET the NetBird REST API. Returns parsed JSON, or None if unconfigured/unreachable."""
    if not NETBIRD_API_TOKEN:
        return None
    try:
        r = httpx.get(NETBIRD_API_URL + path,
                      headers={"Authorization": "Token " + NETBIRD_API_TOKEN, "Accept": "application/json"},
                      timeout=10.0)
        return r.json() if r.status_code < 400 else None
    except Exception:  # noqa: BLE001
        return None


def _nb_post(path: str, body: dict):
    if not NETBIRD_API_TOKEN:
        return None, "NETBIRD_API_TOKEN not set — point NETBIRD_API_URL/TOKEN at a NetBird management API"
    try:
        r = httpx.post(NETBIRD_API_URL + path,
                       headers={"Authorization": "Token " + NETBIRD_API_TOKEN, "Content-Type": "application/json"},
                       json=body, timeout=15.0)
        js = r.json() if (r.headers.get("content-type", "").startswith("application/json")) else {}
        return (js, None) if r.status_code < 400 else (js, f"HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:  # noqa: BLE001
        return None, str(e)


# A clearly-labelled sample so the review demos before a NetBird is wired to the stack.
_NB_SAMPLE_PEERS = [
    {"name": "evo-x2-control", "connected": True, "os": "Ubuntu 24.04", "ip": "100.92.0.1", "login_expired": False, "ssh_enabled": True, "approval_required": False, "groups": [{"name": "control-plane"}]},
    {"name": "proxmox-01", "connected": True, "os": "Debian 12", "ip": "100.92.0.2", "login_expired": False, "ssh_enabled": False, "approval_required": False, "groups": [{"name": "cores"}]},
    {"name": "laptop-alex", "connected": False, "os": "macOS 15", "ip": "100.92.0.7", "login_expired": True, "ssh_enabled": True, "approval_required": False, "groups": [{"name": "admins"}]},
    {"name": "contractor-vm", "connected": True, "os": "Windows 11", "ip": "100.92.0.9", "login_expired": False, "ssh_enabled": True, "approval_required": True, "groups": [{"name": "All"}]},
]
_NB_SAMPLE_POLICIES = [
    {"name": "Default", "enabled": True, "rules": [{"name": "Default", "action": "accept", "enabled": True, "bidirectional": True,
        "sources": [{"name": "All"}], "destinations": [{"name": "All"}], "protocol": "all", "ports": []}]},
    {"name": "admins→cores", "enabled": True, "rules": [{"name": "ssh", "action": "accept", "enabled": True, "bidirectional": False,
        "sources": [{"name": "admins"}], "destinations": [{"name": "cores"}], "protocol": "tcp", "ports": ["22"]}]},
]


def snapshot() -> dict:
    """Live peers+policies when configured, else the labelled sample."""
    peers = _nb_get("/peers")
    if peers is None:
        return {"live": False, "peers": _NB_SAMPLE_PEERS, "policies": _NB_SAMPLE_POLICIES}
    return {"live": True, "peers": peers, "policies": (_nb_get("/policies") or [])}


def group_names(refs) -> list[str]:
    out = []
    for g in refs or []:
        out.append((g.get("name") or g.get("id") or "?") if isinstance(g, dict) else str(g))
    return out


def broad_rule(rule: dict) -> bool:
    """A rule that lets ~everything reach ~everything (accept, enabled) — breaks least-privilege."""
    if not rule.get("enabled", True) or rule.get("action", "accept") != "accept":
        return False
    broad = {"all", "*", "everyone"}
    src = {s.lower() for s in group_names(rule.get("sources"))}
    dst = {d.lower() for d in group_names(rule.get("destinations"))}
    return bool(src & broad) and bool(dst & broad)


def risks(snap: dict) -> list[dict]:
    out: list[dict] = []
    for p in snap["policies"]:
        for rule in p.get("rules") or []:
            if broad_rule(rule):
                out.append({"kind": "broad_policy", "severity": "high",
                            "detail": f"policy '{p.get('name')}' rule '{rule.get('name')}' allows All→All ({rule.get('protocol', 'all')}) — over-broad"})
    for pe in snap["peers"]:
        nm = pe.get("name") or pe.get("hostname") or pe.get("ip") or "?"
        if pe.get("approval_required"):
            out.append({"kind": "unapproved_peer", "severity": "high", "detail": f"peer '{nm}' is pending approval — unapproved device on the network"})
        if pe.get("login_expired"):
            out.append({"kind": "login_expired", "severity": "medium", "detail": f"peer '{nm}' has an expired login — should re-authenticate"})
        if pe.get("ssh_enabled") and "All" in group_names(pe.get("groups")):
            out.append({"kind": "ssh_broad", "severity": "medium", "detail": f"peer '{nm}' has SSH enabled while in the 'All' group"})
    return out


def access_review(_a: dict) -> dict:
    snap = snapshot()
    peers, pol, rk = snap["peers"], snap["policies"], risks(snap)
    conn = sum(1 for p in peers if p.get("connected"))
    tag = "" if snap["live"] else " (SAMPLE — set NETBIRD_API_TOKEN for live data)"
    txt = f"NetBird access review{tag}: {len(peers)} peer(s), {conn} connected, {len(pol)} policy(ies). "
    txt += (f"{len(rk)} risk(s): " + "; ".join(r["detail"] for r in rk[:4]) + "."
            if rk else "No over-broad policies or posture issues found.")
    return {"text": txt, "data": {"live": snap["live"], "peers": len(peers), "connected": conn, "policies": len(pol), "risks": rk}}


def posture(_a: dict) -> dict:
    snap = snapshot()
    issues = [r for r in risks(snap) if r["kind"] in ("login_expired", "unapproved_peer", "ssh_broad")]
    tag = "" if snap["live"] else " (SAMPLE)"
    if not issues:
        return {"text": f"Device posture{tag}: all peers healthy — no expired logins, unapproved devices or broad SSH.", "data": {"issues": []}}
    return {"text": f"Device posture{tag}: {len(issues)} issue(s) — " + "; ".join(i["detail"] for i in issues[:5]) + ".", "data": {"issues": issues}}


def propose(body: dict) -> dict:
    """Draft a tighter NetBird policy — the SENSITIVE action, staged for human approval."""
    snap = snapshot()
    target = (body.get("policy") or "").strip()
    broad = [p for p in snap["policies"] if any(broad_rule(r) for r in (p.get("rules") or []))]
    pick = next((p for p in broad if not target or p.get("name", "").lower() == target.lower()), (broad[0] if broad else None))
    if pick is None:
        return {"status": "noop", "action": "propose_network_policy",
                "summary": "No over-broad policy to tighten — network access is already least-privilege."}
    draft = {"name": f"{pick.get('name', 'Default')} (tightened)", "enabled": True, "rules": [{
        "name": "least-privilege", "action": "accept", "enabled": True, "bidirectional": False,
        "sources": [{"name": "admins"}], "destinations": [{"name": "cores"}], "protocol": "tcp", "ports": ["22", "443"]}]}
    return {"status": "pending_approval", "action": "propose_network_policy", "requires": "human approval",
            "summary": f"tighten NetBird policy '{pick.get('name')}' (All→All ⇒ admins→cores:22,443)",
            "detail": "Draft only — nothing changes until approve_network_policy is called.",
            "replaces": pick.get("name"), "draft": draft}


def approve(body: dict) -> dict:
    """The approved path: actually create the tightened policy via the NetBird API."""
    draft = body.get("draft") or {}
    if not draft:
        return {"status": "error", "action": "approve_network_policy", "error": "missing 'draft' — call propose_network_policy first"}
    res, err = _nb_post("/policies", draft)
    if err:
        return {"status": "error", "action": "approve_network_policy", "error": err,
                "hint": "Set NETBIRD_API_URL + NETBIRD_API_TOKEN to a reachable NetBird management API."}
    return {"status": "done", "action": "approve_network_policy", "enforced": True,
            "summary": f"Created tightened policy '{draft.get('name')}' via NetBird.", "policy": res}


def panel() -> str:
    """Server-rendered peers/policies/risks review page (reuses app.py's page chrome)."""
    from . import app as _app  # deferred — avoids a load-time cycle (app imports this module)

    snap = snapshot()
    prows = []
    for p in snap["peers"]:
        flags = []
        if p.get("approval_required"):
            flags.append("<span style='color:#ff6b6b'>pending approval</span>")
        if p.get("login_expired"):
            flags.append("<span style='color:#e0b000'>login expired</span>")
        if p.get("ssh_enabled"):
            flags.append("<span style='color:#9b99a1'>ssh</span>")
        st = ("<span style='color:#4fd1c5'>connected</span>" if p.get("connected") else "<span style='color:#9b99a1'>offline</span>")
        prows.append([_app._esc(p.get("name") or p.get("hostname") or "?"), _app._esc(p.get("os") or "?"),
                      _app._esc(p.get("ip") or ""), _app._esc(", ".join(group_names(p.get("groups"))) or "—"), st, ", ".join(flags) or "—"])
    polrows = []
    for pol in snap["policies"]:
        for rule in pol.get("rules") or []:
            broad = broad_rule(rule)
            scope = f"{', '.join(group_names(rule.get('sources'))) or '—'} → {', '.join(group_names(rule.get('destinations'))) or '—'}"
            polrows.append([_app._esc(pol.get("name") or "?"),
                            f"<span style='color:{'#ff6b6b' if broad else '#e4e2e6'}'>{_app._esc(scope)}</span>",
                            _app._esc(rule.get("protocol", "all")) + (":" + ",".join(rule.get("ports") or []) if rule.get("ports") else ""),
                            "<span style='color:#ff6b6b'>over-broad</span>" if broad else "<span style='color:#4fd1c5'>scoped</span>"])
    body = (_app._card(f"Peers &nbsp; {_app._live_badge(snap['live'], 'set NETBIRD_API_TOKEN')}",
                       _app._tbl(["Peer", "OS", "Overlay IP", "Groups", "State", "Flags"], prows))
            + _app._card("Access policies", _app._tbl(["Policy", "Source → Destination", "Proto", "Verdict"], polrows))
            + _app._card("Risks &amp; least-privilege", _app._risk_list(risks(snap))
                         + "<p style='color:#9b99a1;font-size:12px;margin:10px 0 0'>Ask the agent to <b>propose a tightened policy</b> — it stages the change and waits for your approval before touching NetBird.</p>"))
    return _app._panel_shell("Network Access", "Zero-trust access review over NetBird — peers, policies and posture, with an approval-gated tighten action.", body)
