"""Stakeholder alerting for the monitoring agent — turn a mission that needs sign-off into an alert.

The value of a long-running monitoring deployment isn't just watching — it's a *fast* path from
"production issue detected" to "a human signs off on the fix". This wires that path: a lifecycle
contributor fires when the runtime reaches a human approval **gate** (a governed response mission is
holding for sign-off) or when a mission **finishes**, and posts to a webhook (Slack-compatible or any
JSON endpoint) with a deep-link to the cockpit. Off unless ``ALERT_WEBHOOK_URL`` is set.

Install on the runtime's lifecycle registry: ``runtime.lifecycle.install(AlertContributor())``.
"""
from __future__ import annotations

import json
import os
import urllib.request

from agentic_os.mission.lifecycle import GateReached, LifecycleContributor, MissionFinished


class Notifier:
    """Posts a message to a webhook (``{"text": ...}`` — Slack-compatible; works with any endpoint
    that accepts a JSON body). No-op (returns False) when no URL is configured."""

    def __init__(self, url: str | None = None, timeout: float = 6.0):
        self.url = url if url is not None else os.environ.get("ALERT_WEBHOOK_URL", "")
        self.timeout = timeout

    def send(self, text: str, **fields) -> bool:
        if not self.url:
            return False
        try:
            req = urllib.request.Request(
                self.url, data=json.dumps({"text": text, **fields}).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=self.timeout)  # noqa: S310 (operator-configured URL)
            return True
        except Exception:  # noqa: BLE001 — a failed alert must never break the mission loop
            return False


class AlertContributor(LifecycleContributor):
    """Alert a stakeholder when a mission holds for sign-off, and when it resolves. Uses the injected
    ``notifier`` capability if the host provided one, else its own. ``cockpit_url`` deep-links the
    approval so a stakeholder can act immediately."""

    def __init__(self, notifier: Notifier | None = None, cockpit_url: str | None = None):
        self.notifier = notifier or Notifier()
        self.cockpit_url = cockpit_url if cockpit_url is not None else os.environ.get("COCKPIT_URL", "")

    def on_gate_reached(self, event: GateReached, capabilities: dict) -> None:
        notifier = capabilities.get("notifier") or self.notifier
        link = f"\nApprove → {self.cockpit_url}/inbox" if self.cockpit_url else ""
        notifier.send(
            f"⏳ Sign-off needed — mission `{event.mission_id}` is holding at `{event.capability}`. "
            f"A human must approve before it runs.{link}",
            mission_id=event.mission_id, capability=event.capability, kind="approval")

    def on_mission_finished(self, event: MissionFinished, capabilities: dict) -> None:
        notifier = capabilities.get("notifier") or self.notifier
        icon = "✅" if event.state == "succeeded" else "❌"
        notifier.send(f"{icon} Mission `{event.mission_id}` {event.state}.",
                      mission_id=event.mission_id, state=event.state, kind="finished")
