"""A synthetic CRM. Executes each idempotency key at most once (the contract a real operator must honour), keeps its effect log on disk so it
survives a simulated process restart (it models the EXTERNAL system), and can lose its answer: the effect happens, the caller sees an error."""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Dict, List


class MockCrm:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self.calls: List[str] = []
        self.lose_answer_once = False        # next write: do the effect, then raise (an unknown outcome for the caller)
        self.fail_before_effect_once = False
        self.slow_s = 0.0
        self.no_receipt = False              # next write: do the effect but return no provider object id
        self._state = self._load()

    def _load(self) -> dict:
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        return {"leads": {f"lead_{n}": {"status": "new", "company": f"Example Co {n}"} for n in ("dana01", "omar02")}, "effects": {}, "sent": {}}

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._state, f)
        os.replace(tmp, self.path)

    @property
    def effect_count(self) -> int:
        return sum(self._state["effects"].values()) if self._state["effects"] else 0

    @property
    def effects(self) -> Dict[str, int]:
        return dict(self._state["effects"])

    def lead_lookup(self, args: dict) -> dict:
        self.calls.append("lookup")
        lead = self._state["leads"].get(args["lead_id"])
        return {"found": lead is not None, "lead": lead}

    def _write(self, kind: str, args: dict, key: str, apply) -> dict:
        self.calls.append(kind)
        if self.slow_s:
            time.sleep(self.slow_s)
        with self._lock:
            if self.fail_before_effect_once:
                self.fail_before_effect_once = False
                raise RuntimeError("synthetic downstream failure before the effect")
            ek = f"{kind}:{key}"
            if ek in self._state["effects"]:                       # idempotent at the provider
                return {"provider_object_id": f"crm_{kind}_{key[-8:]}", "duplicate": True}
            apply()
            self._state["effects"][ek] = 1
            self._save()
            lose, self.lose_answer_once = self.lose_answer_once, False
            nr, self.no_receipt = self.no_receipt, False
        if lose:
            raise RuntimeError("synthetic: the answer was lost after the effect")
        return {} if nr else {"provider_object_id": f"crm_{kind}_{key[-8:]}"}

    def request_lead_update(self, args: dict, key: str) -> dict:
        return self._write("update", args, key, lambda: self._state["leads"][args["lead_id"]].update(status=args["status"]))

    def request_outreach_send(self, args: dict, key: str) -> dict:
        return self._write("send", args, key, lambda: self._state["sent"].__setitem__(key, args["body"]))
