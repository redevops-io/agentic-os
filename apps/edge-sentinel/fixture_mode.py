"""EDGE_SENTINEL_DEMO — a fixture CrowdSec core for demonstration and UI acceptance.

**Why the fixture sits at the LAPI boundary.** `core.fetch_activity()` is where the dashboard's numbers
are actually produced: it merges decisions and alerts, buckets severity, sorts, counts unique sources,
derives the 24-hour window and builds the scenario bars. Faking *that* function would let a demo display
numbers no real read could produce, and would leave the code under acceptance untested. So this module
replaces only the transport — the two reads and the liveness probe — and hands `fetch_activity` raw
payloads in CrowdSec's own shapes. Every KPI, sort order and severity label a reviewer sees is computed
by the production code path from known inputs, which is what makes the demo usable as an acceptance
harness rather than a screenshot.

**What this module deliberately does not do.**

* It does not weaken P9. The bind/exposure guard, the operator-credential requirement and TLS
  verification are read by `security.py` from their own variables; turning the demo on grants no
  exposure and no exemption. A misconfigured demo container still refuses to start.
* It does not touch `seed.py`. Seeding is a separate operator CLI that shells out to `sudo docker exec
  cscli` and rotates bouncer credentials (audit item P6, frozen); demo mode is not a route to it and
  shares no code with it.
* It does not disable writes by hiding buttons. `block_ip`, `unblock_ip` and `_cscli` are replaced on
  the `core` module itself, so no request path — authenticated or not — can reach the CrowdSec core,
  and `test_fixture_mode_refuses_writes.py` proves it with a tripwire rather than by inspection.

Addresses come from RFC 5737's documentation ranges (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24), so
nothing in a fixture, a screenshot or a report can point at a real host.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

DEMO_ENV = "EDGE_SENTINEL_DEMO"
SCENARIO_ENV = "EDGE_SENTINEL_DEMO_SCENARIO"

#: Label the UI and every JSON payload carry, so synthetic data cannot be mistaken for a detection.
SYNTHETIC_TAG = "DEMO / SYNTHETIC DATA"

#: `mixed` renders four of the five stories at once; the fifth is a transport state, not a row.
SCENARIOS = ("mixed", "unavailable")

STORIES: Dict[str, str] = {
    "benign": "Single low-severity event, no decision — the dashboard must not alarm on it.",
    "repeated": "Five events from one source in a short window — the deduplication and severity path.",
    "ambiguous": "A signal with no decision attached — the abstain path; nothing was blocked and the UI says so.",
    "approval": "High-severity RCE with an active ban — the approval/provenance path.",
    "unavailable": "LAPI unreachable — every KPI reads 'not read' rather than zero.",
}


class DemoRefused(RuntimeError):
    """A CrowdSec side effect was attempted while the demo fixture core was installed."""


def _env(env: Optional[dict]) -> dict:
    return os.environ if env is None else env


def enabled(env: Optional[dict] = None) -> bool:
    """Demo mode is opt-in, per-process, and off by default — an unset or empty value is not demo mode."""
    return str(_env(env).get(DEMO_ENV, "")).strip().lower() in ("1", "true", "yes", "on")


def scenario(env: Optional[dict] = None) -> str:
    s = str(_env(env).get(SCENARIO_ENV, "mixed")).strip().lower()
    return s if s in SCENARIOS else "mixed"


def _stamp(offset_seconds: float) -> str:
    """CrowdSec's wire format, relative to now so the 24-hour window and the 'ago' column behave."""
    return (datetime.now(timezone.utc) - timedelta(seconds=offset_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── raw fixtures, in the shapes the LAPI and cscli actually return ────────────────────────────────────

def fixture_decisions() -> List[dict]:
    """GET /v1/decisions payload: {value, scope, scenario, type, duration, origin}."""
    return [
        {"source": "crowdsec", "scenario": "crowdsecurity/http-cve-2021-41773-rce",
         "scope": "Ip", "value": "192.0.2.53", "type": "ban", "duration": "4h0m0s", "origin": "crowdsec",
         "simulated": True},
        {"source": "crowdsec", "scenario": "crowdsecurity/ssh-bf",
         "scope": "Ip", "value": "198.51.100.7", "type": "ban", "duration": "1h30m0s", "origin": "crowdsec",
         "simulated": True},
    ]


def fixture_alerts() -> List[dict]:
    """`cscli alerts list -o json` payload: {scenario, source{value,scope}, events_count, created_at, decisions[]}."""
    return [
        # story: approval — high severity, decision attached, an active ban above
        {"scenario": "crowdsecurity/http-cve-2021-41773-rce", "events_count": 2,
         "created_at": _stamp(7 * 60), "source": {"value": "192.0.2.53", "scope": "Ip", "ip": "192.0.2.53"},
         "decisions": [{"origin": "crowdsec", "type": "ban", "scope": "Ip", "value": "192.0.2.53"}],
         "simulated": True},
        # story: repeated — one source, five events in a short window
        {"scenario": "crowdsecurity/ssh-bf", "events_count": 5,
         "created_at": _stamp(42 * 60), "source": {"value": "198.51.100.7", "scope": "Ip", "ip": "198.51.100.7"},
         "decisions": [{"origin": "crowdsec", "type": "ban", "scope": "Ip", "value": "198.51.100.7"}],
         "simulated": True},
        # story: ambiguous — no decision at all; the honest abstain case
        {"scenario": "custom/unclear-signal-needs-review", "events_count": 1,
         "created_at": _stamp(3 * 3600), "source": {"value": "203.0.113.9", "scope": "Ip", "ip": "203.0.113.9"},
         "decisions": [], "simulated": True},
        # story: benign — one low-severity event, nothing to act on
        {"scenario": "crowdsecurity/tor-exit-node", "events_count": 1,
         "created_at": _stamp(26 * 3600), "source": {"value": "192.0.2.11", "scope": "Ip", "ip": "192.0.2.11"},
         "decisions": [], "simulated": True},
    ]


# ── installation: swap the transport, refuse the side effects ─────────────────────────────────────────

_INSTALLED: Dict[str, object] = {}          # name -> original, non-empty only while installed


def _refusing(core, what: str) -> Callable[..., dict]:
    def _refuse(*args, **kwargs):
        raise DemoRefused(
            f"{what} refused: {DEMO_ENV} is on, so no CrowdSec side effect is permitted. "
            "Unset the variable to operate against a real core.")
    return _refuse


def install(env: Optional[dict] = None) -> dict:
    """Point the app's CrowdSec reads at fixtures and refuse its writes. Idempotent.

    Returns a report of what was changed, which `/health` surfaces so evidence can never be ambiguous
    about which mode produced it.
    """
    from . import core

    if _INSTALLED:
        return {"installed": True, "already": True, "scenario": scenario(env), "patched": sorted(_INSTALLED)}

    mode = scenario(env)
    note = f"demo: {SYNTHETIC_TAG} ({mode})"

    def _connected() -> bool:
        return mode != "unavailable"

    def _fetch_decisions() -> List[dict]:
        if mode == "unavailable":
            core._set_status(core._DECISIONS_STATUS, "decisions", ok=False, note=note)
            return []
        # built fresh on every call: callers sort and annotate the list, and one read must not mutate the next
        core._set_status(core._DECISIONS_STATUS, "decisions", ok=True, note=note)
        return fixture_decisions()

    def _fetch_alerts() -> List[dict]:
        if mode == "unavailable":
            core._set_status(core._ALERTS_STATUS, "alerts", ok=False, note=note)
            return []
        core._set_status(core._ALERTS_STATUS, "alerts", ok=True, note=note)
        return list(fixture_alerts())

    patch: Dict[str, object] = {
        "crowdsec_connected": _connected,
        "fetch_decisions": _fetch_decisions,
        "fetch_alerts": _fetch_alerts,
        # the side effects, refused at the module the handlers call into — not in the UI
        "block_ip": _refusing(core, "block_ip"),
        "unblock_ip": _refusing(core, "unblock_ip"),
        "_cscli": _refusing(core, "cscli"),
    }
    for name, replacement in patch.items():
        _INSTALLED[name] = getattr(core, name)
        setattr(core, name, replacement)

    core._CACHE.update(ts=0.0, data=None)              # drop any live read cached before the switch
    return {"installed": True, "already": False, "scenario": mode,
            "patched": sorted(patch), "synthetic": True, "tag": SYNTHETIC_TAG}


def uninstall() -> dict:
    """Restore the originals (test teardown; also lets one process leave demo mode)."""
    from . import core
    for name, original in list(_INSTALLED.items()):
        setattr(core, name, original)
        del _INSTALLED[name]
    core._CACHE.update(ts=0.0, data=None)
    return {"installed": False, "restored": True}


def status() -> dict:
    """Current mode, for /health and for the dashboard banner."""
    return {"enabled": bool(_INSTALLED), "tag": SYNTHETIC_TAG if _INSTALLED else ""}


def install_if_enabled(env: Optional[dict] = None) -> Optional[dict]:
    """Install when `EDGE_SENTINEL_DEMO` is set; return None (and change nothing) otherwise."""
    return install(env) if enabled(env) else None

