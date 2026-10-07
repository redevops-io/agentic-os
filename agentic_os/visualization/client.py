"""Metabase write client — the capability the apps stack was missing.

A ``MetabaseWriter`` Protocol plus two implementations: ``HttpMetabaseWriter`` (session-auth REST, the real
thing) and ``InMemoryMetabaseWriter`` (a fake for tests and fake-until-credentialed deploys). Both expose the
same four calls the governed layer needs: create a card, create a dashboard, place dashcards, and — crucially —
``get_card`` for an INDEPENDENT read-back so "created" means the card actually exists, not merely that a POST
returned 2xx (the same discipline as the obligation engine).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol, Sequence, runtime_checkable


@runtime_checkable
class MetabaseWriter(Protocol):
    def create_card(self, payload: Dict[str, Any]) -> Dict[str, Any]: ...
    def create_dashboard(self, name: str) -> Dict[str, Any]: ...
    def add_dashcards(self, dashboard_id: int, card_ids: Sequence[int]) -> bool: ...
    def get_card(self, card_id: int) -> Optional[Dict[str, Any]]: ...   # read-back verification


@dataclass
class InMemoryMetabaseWriter:
    """A fake Metabase for tests / fake-until-credentialed. ``drop_writes`` simulates the silent-failure case:
    the POST 'succeeds' but nothing is persisted, so the read-back catches it."""
    drop_writes: bool = False
    _cards: Dict[int, Dict[str, Any]] = field(default_factory=dict)
    _dashboards: Dict[int, Dict[str, Any]] = field(default_factory=dict)
    _seq: int = 0

    def create_card(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._seq += 1
        cid = self._seq
        rec = {"id": cid, "name": payload.get("name", ""), "display": payload.get("display", "")}
        if not self.drop_writes:
            self._cards[cid] = rec
        return rec

    def create_dashboard(self, name: str) -> Dict[str, Any]:
        self._seq += 1
        did = self._seq
        self._dashboards[did] = {"id": did, "name": name, "dashcards": []}
        return self._dashboards[did]

    def add_dashcards(self, dashboard_id: int, card_ids: Sequence[int]) -> bool:
        d = self._dashboards.get(dashboard_id)
        if d is None:
            return False
        d["dashcards"] = list(card_ids)
        return True

    def get_card(self, card_id: int) -> Optional[Dict[str, Any]]:
        return self._cards.get(card_id)

    # ── workspace CRUD (MetabaseWorkspaceProvider) ──
    def update_card(self, card_id: int, changes: Dict[str, Any]) -> Dict[str, Any]:
        rec = self._cards.get(card_id)
        if rec is None:
            return {}
        rec.update({k: v for k, v in changes.items() if k != "id"})
        return rec

    def archive_card(self, card_id: int) -> bool:
        rec = self._cards.get(card_id)
        if rec is None:
            return False
        rec["archived"] = True
        return True

    def list_cards(self) -> list:
        return [c for c in self._cards.values() if not c.get("archived")]

    def get_dashboard(self, dashboard_id: int) -> Optional[Dict[str, Any]]:
        return self._dashboards.get(dashboard_id)

    def update_dashboard(self, dashboard_id: int, changes: Dict[str, Any]) -> Dict[str, Any]:
        d = self._dashboards.get(dashboard_id)
        if d is None:
            return {}
        d.update({k: v for k, v in changes.items() if k != "id"})
        return d

    def archive_dashboard(self, dashboard_id: int) -> bool:
        d = self._dashboards.get(dashboard_id)
        if d is None:
            return False
        d["archived"] = True
        return True

    def add_card_to_dashboard(self, dashboard_id: int, card_id: int, *, size_x: int = 12, size_y: int = 4) -> bool:
        d = self._dashboards.get(dashboard_id)
        if d is None:
            return False
        cards = d.setdefault("dashcards", [])
        if card_id not in cards:
            cards.append(card_id)
        return True


@dataclass
class HttpMetabaseWriter:
    """Real Metabase REST writer. Authenticates with admin creds (``POST /api/session`` → X-Metabase-Session) and
    self-heals an expired token on a 401, mirroring control-tower/core.py."""
    base_url: str
    email: str = ""
    password: str = ""
    timeout: float = 15.0
    _token: str = ""

    def _client(self):
        import httpx  # local import so the package stays importable without httpx
        return httpx.Client(base_url=self.base_url.rstrip("/"), timeout=self.timeout)

    def _login(self, c) -> None:
        r = c.post("/api/session", json={"username": self.email, "password": self.password})
        r.raise_for_status()
        self._token = r.json()["id"]

    def _req(self, c, method: str, path: str, json: Any = None):
        if not self._token:
            self._login(c)
        headers = {"X-Metabase-Session": self._token}
        r = c.request(method, path, json=json, headers=headers)
        if r.status_code == 401:                     # token expired → re-login once
            self._login(c)
            headers["X-Metabase-Session"] = self._token
            r = c.request(method, path, json=json, headers=headers)
        return r

    def create_card(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self._client() as c:
            r = self._req(c, "POST", "/api/card", json=payload)
            r.raise_for_status()
            return r.json()

    def create_dashboard(self, name: str) -> Dict[str, Any]:
        with self._client() as c:
            r = self._req(c, "POST", "/api/dashboard", json={"name": name})
            r.raise_for_status()
            return r.json()

    def add_dashcards(self, dashboard_id: int, card_ids: Sequence[int]) -> bool:
        from .compile import dashcards_payload
        with self._client() as c:
            r = self._req(c, "PUT", f"/api/dashboard/{dashboard_id}",
                          json={"dashcards": dashcards_payload(card_ids)})
            return r.status_code in (200, 201)

    def get_card(self, card_id: int) -> Optional[Dict[str, Any]]:
        with self._client() as c:
            r = self._req(c, "GET", f"/api/card/{card_id}")
            return r.json() if r.status_code == 200 else None

    # ── workspace CRUD (MetabaseWorkspaceProvider) ──
    def update_card(self, card_id: int, changes: Dict[str, Any]) -> Dict[str, Any]:
        with self._client() as c:
            r = self._req(c, "PUT", f"/api/card/{card_id}", json=changes)
            r.raise_for_status()
            return r.json()

    def archive_card(self, card_id: int) -> bool:
        with self._client() as c:                            # Metabase soft-deletes via the archived flag
            r = self._req(c, "PUT", f"/api/card/{card_id}", json={"archived": True})
            return r.status_code in (200, 201)

    def list_cards(self) -> list:
        with self._client() as c:
            r = self._req(c, "GET", "/api/card")
            return r.json() if r.status_code == 200 else []

    def get_dashboard(self, dashboard_id: int) -> Optional[Dict[str, Any]]:
        with self._client() as c:
            r = self._req(c, "GET", f"/api/dashboard/{dashboard_id}")
            return r.json() if r.status_code == 200 else None

    def update_dashboard(self, dashboard_id: int, changes: Dict[str, Any]) -> Dict[str, Any]:
        with self._client() as c:
            r = self._req(c, "PUT", f"/api/dashboard/{dashboard_id}", json=changes)
            r.raise_for_status()
            return r.json()

    def archive_dashboard(self, dashboard_id: int) -> bool:
        with self._client() as c:
            r = self._req(c, "PUT", f"/api/dashboard/{dashboard_id}", json={"archived": True})
            return r.status_code in (200, 201)

    def add_card_to_dashboard(self, dashboard_id: int, card_id: int, *, size_x: int = 12, size_y: int = 4) -> bool:
        """Append a card to an EXISTING dashboard, preserving its current dashcards (Metabase replaces the whole
        dashcards set on PUT, so we read-modify-write)."""
        with self._client() as c:
            cur = self._req(c, "GET", f"/api/dashboard/{dashboard_id}")
            if cur.status_code != 200:
                return False
            existing = cur.json().get("dashcards", []) or []
            row = (len(existing) // 2) * size_y
            col = (len(existing) % 2) * size_x
            new = {"id": -(len(existing) + 1), "card_id": card_id, "row": row, "col": col,
                   "size_x": size_x, "size_y": size_y}
            r = self._req(c, "PUT", f"/api/dashboard/{dashboard_id}", json={"dashcards": existing + [new]})
            return r.status_code in (200, 201)


__all__ = ["MetabaseWriter", "InMemoryMetabaseWriter", "HttpMetabaseWriter"]
