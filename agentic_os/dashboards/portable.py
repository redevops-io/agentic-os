"""The dashboards with no server: the same service behind one function instead of HTTP routes.

A browser tab running WebAssembly Python (Pyodide), or any other host without a web framework, calls

    api = PortableDashboards(data_dir="/data")
    status, body = api.handle("GET", "/api/dashboards/qa", {"days": "30"})

and gets what the HTTP route of the same path returns (``routes.py``), built by the same ``DashboardService``. Only the
standard library, ``pyyaml`` and ``sqlite3`` are imported.

What works here is what needs no connected application: the dashboards built from test results reported INTO this store
(QA, portfolio) and the ones that describe the installation itself (missions, infrastructure - both say that there is
nothing to read). The dashboards of connected applications (CRM, accounting, support, ...) are NOT available: reading
them takes that application's credential, and a credential in a web page belongs to everyone who opens the page. They
are listed by ``GET /api/dashboards/_portable`` with what each one needs, and asked for directly they explain it.

There is one person here (whoever opened the page), so every request is that person's: ``local-owner``.
"""
from __future__ import annotations

import os
import re
import tempfile
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .config import parse_config
from .service import BY_ID, DEFINITIONS, DashboardService, Forbidden, NotFound, parse_filters
from .sources import SOURCE_META
from .store import DashboardStore, StoreError

OWNER = "local-owner"
_VIEW = re.compile(r"^view-[0-9a-f]{16}$")
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
#: the dashboards that can show something without a connected application
STANDALONE = ("portfolio", "qa", "missions", "infrastructure")
MAX_JUNIT_BYTES = 16_000_000


class _LocalProvider:
    """What a page-only installation has: no runtime, no products, no applications."""
    serves_fixtures = False

    def __init__(self, projects: List[Dict[str, str]]) -> None:
        self._projects = projects

    def projects(self) -> List[Dict[str, str]]:
        return list(self._projects)

    def attention(self, pid: str) -> list:
        return []

    def missions(self, pid: str) -> list:
        return []

    def sources(self, pid: str) -> list:
        return []

    def runtime(self, pid: str) -> Dict[str, list]:
        return {"runtimes": [], "models": []}


def _err(status: int, code: str, detail: str) -> Tuple[int, Dict[str, Any]]:
    return status, {"detail": {"error": code, "detail": detail}}


class PortableDashboards:
    def __init__(self, data_dir: str = "", projects: Optional[List[Dict[str, str]]] = None) -> None:
        self.data_dir = data_dir
        self._projects = [dict(p) for p in (projects or [{"id": "local", "name": "This browser"}])]
        self._suites: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._make()

    def _make(self) -> None:
        cfg = parse_config({"schema": "aos.dashboards/v1", "projects": [{"id": p["id"], "name": p["name"]} for p in self._projects],
                            "qa": {"suites": list(self._suites.values())}})
        self.svc = DashboardService(_LocalProvider(self._projects), config=cfg, store=DashboardStore(self.data_dir))
        self.acc = self.svc.access(OWNER)

    # ── what the page shows about itself ────────────────────────────────────────────────────────
    def describe(self) -> Dict[str, Any]:
        offered = {d["id"] for d in self.svc.catalog(self.acc)["dashboards"]}
        out: Dict[str, Any] = {"mode": "standalone", "offered": sorted(offered), "projects": list(self._projects),
                               "durable": bool(self.data_dir), "available": [], "unavailable": []}
        for df in DEFINITIONS:
            needs = sorted({SOURCE_META[sid][0] for sid in df.needs_any})
            if df.id in STANDALONE:
                out["available"].append({"id": df.id, "title": df.title, "offered": df.id in offered,
                                         "note": "" if df.id in offered else "appears when test results have been added"
                                         if df.id == "qa" else "nothing to read in this browser yet"})
            else:
                out["unavailable"].append({
                    "id": df.id, "title": df.title, "needs": needs,
                    "why": f"Reads {', '.join(needs)} with that application's credential. A credential is never placed in this "
                           "page; connect to a Projects deployment that holds it to see this dashboard."})
        return out

    def _not_offered_here(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        """The served catalogue tells an operator which setting to change and to restart. A page has no settings and no
        server: it says what THIS page can do about it instead."""
        out = {k: entry[k] for k in ("id", "title", "priority", "about", "connect") if k in entry}
        if entry["id"] == "qa":
            out["why"] = "No test results have been added in this browser yet."
            out["needs"] = [{"id": "qa_ingest", "label": SOURCE_META["qa_ingest"][0], "state": "not_connected", "detail": "",
                             "remedy": "use \"Add test results\" on this page with a JUnit XML file"}]
            return out
        df = BY_ID[entry["id"]]
        out["why"] = "This page cannot read it: that takes the application's credential, and a credential is never placed in a web page."
        out["needs"] = [{"id": sid, "label": SOURCE_META[sid][0], "state": "not_connected", "detail": "",
                         "remedy": "connect this page to a Projects deployment that holds this connection"} for sid in df.needs_any]
        return out

    # ── one entry point ─────────────────────────────────────────────────────────────────────────
    def handle(self, method: str, path: str, query: Optional[Mapping[str, Any]] = None, body: Any = None) -> Tuple[int, Dict[str, Any]]:
        method, q = method.upper(), {str(k): str(v) for k, v in (query or {}).items() if v is not None}
        parts = [p for p in path.split("?")[0].strip("/").split("/") if p]
        if parts[:2] != ["api", "dashboards"]:
            return _err(404, "not_found", "this page serves the dashboards only")
        rest = parts[2:]
        try:
            return self._route(method, rest, q, body if isinstance(body, dict) else {})
        except NotFound:
            return _err(404, "not_found", "no such dashboard, project or record list")
        except Forbidden:
            return _err(403, "forbidden", "you do not have access to this dashboard or project")
        except StoreError as e:
            return e.status, {"detail": e.as_dict()}

    def _route(self, method: str, rest: List[str], q: Dict[str, str], body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        svc, acc = self.svc, self.acc
        if not rest and method == "GET":
            out = svc.catalog(acc)
            out["access"]["can_save"] = True
            out["not_connected"] = [self._not_offered_here(e) for e in out["not_connected"]]
            return 200, out
        if rest == ["_portable"] and method == "GET":
            return 200, self.describe()
        if rest == ["views"]:
            if method == "GET":
                return 200, {"views": svc.store.views(f"user:{OWNER}", q.get("dashboard", "")[:80]), "durable": svc.store.durable}
            if method == "PUT":
                return self._save_view(body)
        if len(rest) == 2 and rest[0] == "views" and method == "DELETE":
            if not _VIEW.match(rest[1]) or not svc.store.delete_view(f"user:{OWNER}", rest[1]):
                return _err(404, "not_found", "no such view")
            return 200, {"deleted": rest[1]}
        if rest == ["qa", "results"] and method == "POST":
            return self._ingest(body)
        if rest == ["qa", "junit"] and method == "POST":
            return self._ingest_junit(body)
        if len(rest) == 1 and method == "GET":
            fresh = q.pop("fresh", "") in ("1", "true")
            d = svc.build(rest[0], parse_filters(q, now=svc.clock()), acc, fresh=fresh).to_dict()
            if rest[0] not in STANDALONE:
                # say why in this page's own terms: "cannot be read right now" would suggest that waiting helps
                d["empty"] = next(u["why"] for u in self.describe()["unavailable"] if u["id"] == rest[0])
            return 200, d
        if len(rest) == 2 and rest[1] == "drill" and method == "GET":
            key = q.pop("key", "")
            if not key or len(key) > 400:
                return _err(404, "not_found", "no such dashboard, project or record list")
            return 200, svc.drill(rest[0], key, parse_filters(q, now=svc.clock()), acc)
        if len(rest) == 2 and rest[1] == "track" and method == "POST":
            return _err(501, "unsupported", "There is no intake in this browser. Connect to a Projects deployment to track a finding.")
        return _err(404, "not_found", "no such route")

    def _save_view(self, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        extra = set(body) - {"id", "name", "dashboard", "filters", "layout", "is_default"}
        dash, vid = body.get("dashboard"), body.get("id") or ""
        if extra or dash not in BY_ID or not isinstance(body.get("filters", {}), dict) or not isinstance(body.get("layout", {}), dict) \
                or not isinstance(body.get("is_default", False), bool) or (vid and not (isinstance(vid, str) and _VIEW.match(vid))):
            return _err(422, "invalid", "body must be {name, dashboard, filters?, layout?, is_default?, id?}")
        f = body.get("filters") or {}
        keep = {k: f[k] for k in ("project", "days", "since", "until", "product", "environment", "platform") if f.get(k) not in (None, "")}
        return 200, self.svc.store.save_view(f"user:{OWNER}", name=body.get("name") or "", dashboard=dash, filters=keep,
                                             layout=body.get("layout") or {}, is_default=bool(body.get("is_default", False)),
                                             view_id=vid, now=self.svc.clock())

    # ── test results added by the person ────────────────────────────────────────────────────────
    def _admit(self, body: Dict[str, Any]) -> Optional[Tuple[int, Dict[str, Any]]]:
        """A project or suite named for the first time is created: there is no operator file to edit in a page."""
        project, suite = str(body.get("project") or ""), str(body.get("suite") or "")
        if not _ID.match(project) or not _ID.match(suite):
            return _err(422, "invalid", "project and suite must be short lower-case ids (letters, digits, - and _)")
        changed = False
        if project not in {p["id"] for p in self._projects}:
            self._projects.append({"id": project, "name": str(body.get("project_name") or project)[:80]})
            changed = True
        platforms = body.get("platforms")
        if isinstance(platforms, list) and platforms:
            want = {"id": suite, "project": project, "name": str(body.get("suite_name") or suite)[:80],
                    "platforms": [str(p)[:60] for p in platforms][:20]}
            if self._suites.get((project, suite)) != want:
                self._suites[(project, suite)] = want
                changed = True
        if changed:
            self._make()
        return None

    def _ingest(self, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        refused = self._admit(body)
        if refused:
            return refused
        keep = {k: v for k, v in body.items() if k not in ("project_name", "suite_name", "platforms")}
        out = self.svc.store.ingest_qa(keep, by=f"user:{OWNER}", now=self.svc.clock())
        self.svc.cache.invalidate()
        return 200, {**out, "recorded_by": f"user:{OWNER}"}

    def _ingest_junit(self, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        """{project, suite, platform?, run_id?, release?, environment?, platforms?, xml}: one JUnit file as text."""
        from . import report_junit  # noqa: PLC0415
        xml = body.get("xml")
        if not isinstance(xml, str) or not xml.strip():
            return _err(422, "invalid", "xml must be the text of a JUnit result file")
        if len(xml.encode("utf-8", "replace")) > MAX_JUNIT_BYTES:
            return _err(413, "too_large", "the result file is larger than 16 MB")
        fd, tmp = tempfile.mkstemp(suffix=".xml")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(xml)
            try:
                results = report_junit.parse_junit(tmp)
            except Exception:      # noqa: BLE001  (whatever the parser objects to, the person gets one plain sentence)
                return _err(422, "invalid", "this is not a JUnit XML result file")
        finally:
            os.unlink(tmp)
        if not results:
            return _err(422, "invalid", "the file contains no test case")
        now = self.svc.clock()
        run = {k: body[k] for k in ("project", "suite", "platform", "release", "environment", "project_name", "suite_name", "platforms") if body.get(k)}
        run.setdefault("platform", "unknown")
        run["run_id"] = str(body.get("run_id") or f"file-{int(now)}")[:120]
        run["results"] = results
        return self._ingest(run)


__all__ = ["PortableDashboards", "STANDALONE", "OWNER"]
