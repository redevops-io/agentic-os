"""An ephemeral, in-process mock CRM and handoff service. Fixtures only; no network; no real data.

It models the EXTERNAL system: it keeps its own record of every effect and honours an idempotency key (the
contract a downstream system should offer), so a test can ask it, independently of the bridge, how many effects
really happened. State is kept in memory and, when a path is given, in a JSON file in a throwaway directory so
that a simulated process restart does not forget what the 'outside world' already did.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional

FIXTURE_LEADS = {
    "lead_dana01": {"lead_id": "lead_dana01", "name": "Dana Example", "company": "Northwind Example Co", "status": "new",
                    "owner": "sales-team-a", "source": "web_form"},
    "lead_omar02": {"lead_id": "lead_omar02", "name": "Omar Example", "company": "Contoso Example Ltd", "status": "new",
                    "owner": "sales-team-b", "source": "web_form"},
}


class MockCrm:
    def __init__(self, path: Optional[str] = None, *, credential: str = "SENTINEL-AAAAAAAAAAAAAAAAAAAA"):
        self._path = path
        self._lock = threading.RLock()
        self.credential = credential                # a fake stand-in for a backend credential: it must never reach the model, a frame or a log
        self.leads: Dict[str, dict] = {k: dict(v) for k, v in FIXTURE_LEADS.items()}
        self.effects: Dict[str, dict] = {}          # idempotency key -> the effect that really happened
        self.effect_count = 0
        self.calls: List[dict] = []                 # every call that reached the CRM, effect or not
        self.service_s: List[float] = []            # time spent inside the CRM per call (to separate it from governance time)
        self.fail_before_effect: Optional[BaseException] = None     # the call fails and nothing happens
        self.fail_after_effect: Optional[BaseException] = None      # the effect happens, then the caller sees an error (lost response)
        self.gate: Optional[threading.Event] = None                 # when set, a write blocks in the CRM until the event is set
        self.entered = threading.Event()                            # set when a gated call is inside the CRM
        self.read_gate: Optional[threading.Event] = None            # when set, a lookup blocks in the CRM until the event is set
        self.read_entered = threading.Event()
        if path and os.path.exists(path):
            self._load()

    # ── persistence of the 'outside world' across a simulated restart ────────────────────────────────────────
    def _save(self) -> None:
        if self._path:
            with open(self._path, "w", encoding="utf-8") as fh:
                json.dump({"leads": self.leads, "effects": self.effects, "effect_count": self.effect_count}, fh, sort_keys=True)

    def _load(self) -> None:
        with open(self._path, encoding="utf-8") as fh:
            d = json.load(fh)
        self.leads, self.effects, self.effect_count = d["leads"], d["effects"], d["effect_count"]

    # ── helpers ──────────────────────────────────────────────────────────────────────────────────────────────
    def _timed(self, name: str, args: dict, fn: Callable[[], dict]) -> dict:
        t0 = time.perf_counter()
        try:
            return fn()
        finally:
            self.service_s.append(time.perf_counter() - t0)

    def _record(self, name: str, args: dict, key: str) -> None:
        self.calls.append({"call": name, "key": key, "lead": args.get("lead_id") or args.get("contact", "")})

    def _apply_once(self, kind: str, key: str, args: dict, make: Callable[[], dict]) -> dict:
        with self._lock:
            self._record(kind, args, key)
            if self.fail_before_effect is not None:
                exc, self.fail_before_effect = self.fail_before_effect, None
                raise exc
            if self.gate is not None:
                self.entered.set()
                self.gate.wait(10)
            effect_key = f"{kind}:{key}" if key else f"{kind}:nokey:{len(self.calls)}"
            if effect_key in self.effects:                      # the downstream honours the key: same receipt, no second effect
                return dict(self.effects[effect_key])
            receipt = make()
            self.effects[effect_key] = receipt
            self.effect_count += 1
            self._save()
            if self.fail_after_effect is not None:
                exc, self.fail_after_effect = self.fail_after_effect, None
                raise exc
            return dict(receipt)

    # ── CrmPort ──────────────────────────────────────────────────────────────────────────────────────────────
    def lead_lookup(self, arguments: dict) -> dict:
        def go() -> dict:
            self._record("lead_lookup", arguments, "")
            if self.read_gate is not None:
                self.read_entered.set()
                self.read_gate.wait(10)
            lead = self.leads.get(arguments["lead_id"])
            if lead is None:
                return {"found": False}
            return {"found": True, "lead_id": lead["lead_id"], "name": lead["name"], "company": lead["company"], "status": lead["status"]}
        return self._timed("lead_lookup", arguments, go)

    def request_lead_update(self, arguments: dict, idempotency_key: str, envelope: object) -> dict:
        assert envelope is not None, "a write must carry a governed envelope"

        def make() -> dict:
            lead = self.leads[arguments["lead_id"]]
            lead.update({k: v for k, v in arguments.items() if k != "lead_id"})
            return {"provider_object_id": f"crm_upd_{self.effect_count + 1:04d}", "lead_id": lead["lead_id"], "applied": True}
        return self._timed("lead_update", arguments, lambda: self._apply_once("lead_update", idempotency_key, arguments, make))

    def prepare_outreach(self, arguments: dict) -> dict:
        def go() -> dict:
            self._record("prepare_outreach", arguments, "")
            lead = self.leads.get(arguments["contact"], {"name": "there"})
            first = str(lead.get("name", "there")).split()[0]
            return {"draft": f"Hi {first}, thanks for speaking with us today. {arguments['goal']}", "prepared_ref": "prep_" + arguments["contact"][-6:]}
        return self._timed("prepare_outreach", arguments, go)

    def send_outreach(self, arguments: dict, idempotency_key: str, envelope: object) -> dict:
        assert envelope is not None, "a send must carry a governed envelope"

        def make() -> dict:
            return {"provider_object_id": f"msg_{self.effect_count + 1:04d}", "sent": True, "contact": arguments["contact"]}
        return self._timed("send_outreach", arguments, lambda: self._apply_once("send_outreach", idempotency_key, arguments, make))

    # ── HandoffPort ──────────────────────────────────────────────────────────────────────────────────────────
    def request_human(self, arguments: dict, idempotency_key: str, envelope: object) -> dict:
        def make() -> dict:
            return {"provider_object_id": f"task_{self.effect_count + 1:04d}", "queued": True}
        return self._timed("handoff", arguments, lambda: self._apply_once("handoff", idempotency_key, arguments, make))

    # ── ground truth for assertions ──────────────────────────────────────────────────────────────────────────
    def effects_of(self, kind: str) -> List[dict]:
        return [v for k, v in self.effects.items() if k.startswith(kind + ":")]
