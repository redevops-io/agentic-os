"""The dashboards operator file: projects, how records are attributed to them, the QA traceability catalog, which
business sources may be read, and who may see what.

Named by ``AGENTIC_OS_DASHBOARDS`` (YAML or JSON). Nothing in it comes from a request. Unset is a valid deployment: the
dashboards then cover the one project the provider reports, read only what Projects already shows (missions, attention,
the registered products) and enable no business source.

    schema: aos.dashboards/v1
    data_dir: /var/lib/agentic-os/dashboards        # metric history, saved views, ingested QA results (sqlite)
    projects:
      - id: storefront
        name: Storefront
        product: commerce
        match:                                       # every key optional; a record matching none stays unattributed
          qa_targets: [tgt-0123456789ab]
          qa_suites: [checkout-web]
          sentinel_assets: ["203.0.113.*", "storefront-*"]
          missions: ["Customer Refunds", "refund*"]
    sources:                                         # business sources are OFF unless listed here
      twenty: {project: storefront}                  # attribute a single-tenant app to one project ("" = portfolio only)
      erpnext: {}
      metabase: {cards: {revenue_by_month: 12}, dashboards: [{title: Revenue, id: 3}]}
    qa:
      inventory_days: 30                             # a test seen in this window is part of the planned inventory
      suites:
        - id: checkout-web
          project: storefront
          name: Checkout (web)
          environment: staging
          platforms: [chromium-linux, android-17]
          targets: [tgt-0123456789ab]
          profiles: [browser]
          tests:
            - {id: checkout.discount.applied, requirements: [REQ-12], platforms: [chromium-linux]}
      requirements:
        - {id: REQ-12, title: Discounts apply once, project: storefront, priority: p0}
    access:                                          # absent = the boundary the other read routes already have
      anonymous: {projects: [storefront], sources: [qa, missions]}
      principals:
        "approver:acme": {projects: "*", sources: "*"}

Fail closed: an unknown key, a malformed id or a duplicate is an error listed in ``DashboardsConfig.errors`` and the
entry is dropped; it never widens what is read or shown.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

SCHEMA = "aos.dashboards/v1"
ENV = "AGENTIC_OS_DASHBOARDS"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
BUSINESS_SOURCES = ("twenty", "erpnext", "chatwoot", "lago", "umami", "metabase", "postiz", "listmonk", "mission_plane", "sim_runs")
_TOP = {"schema", "data_dir", "projects", "sources", "qa", "access"}
_PROJECT = {"id", "name", "product", "environment", "match"}
_MATCH = {"qa_targets", "qa_suites", "sentinel_assets", "missions", "products"}
_SUITE = {"id", "project", "name", "environment", "platforms", "targets", "profiles", "tests", "product"}
_TEST = {"id", "requirements", "platforms", "title"}
_REQ = {"id", "title", "project", "priority"}
_GRANT = {"projects", "sources"}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ProjectSpec:
    id: str
    name: str
    product: str = ""
    environment: str = ""
    qa_targets: Tuple[str, ...] = ()
    qa_suites: Tuple[str, ...] = ()
    sentinel_assets: Tuple[str, ...] = ()
    missions: Tuple[str, ...] = ()
    products: Tuple[str, ...] = ()       # product ids (registry) wholly owned by this project


@dataclass(frozen=True)
class TestSpec:
    id: str
    title: str = ""
    requirements: Tuple[str, ...] = ()
    platforms: Tuple[str, ...] = ()      # () = every platform of the suite


@dataclass(frozen=True)
class SuiteSpec:
    id: str
    project: str
    name: str
    environment: str = ""
    product: str = ""
    platforms: Tuple[str, ...] = ()
    targets: Tuple[str, ...] = ()
    profiles: Tuple[str, ...] = ()
    tests: Tuple[TestSpec, ...] = ()


@dataclass(frozen=True)
class RequirementSpec:
    id: str
    title: str
    project: str = ""
    priority: str = ""


@dataclass(frozen=True)
class Grant:
    projects: Optional[Tuple[str, ...]] = None      # None = all
    sources: Optional[Tuple[str, ...]] = None       # None = all

    def to_dict(self) -> Dict[str, Any]:
        return {"projects": "*" if self.projects is None else list(self.projects),
                "sources": "*" if self.sources is None else list(self.sources)}


ALL = Grant()
NONE = Grant(projects=(), sources=())


@dataclass
class DashboardsConfig:
    source: str = ""
    data_dir: str = ""
    projects: Dict[str, ProjectSpec] = field(default_factory=dict)
    sources: Dict[str, Dict[str, Any]] = field(default_factory=dict)      # enabled business sources -> options
    suites: Dict[str, SuiteSpec] = field(default_factory=dict)
    requirements: Dict[str, RequirementSpec] = field(default_factory=dict)
    inventory_days: int = 30
    access_configured: bool = False
    anonymous: Grant = field(default_factory=lambda: NONE)
    principals: Dict[str, Grant] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)

    # ── attribution ───────────────────────────────────────────────────────────────────────────────
    def project_for_qa(self, *, target: str = "", suite: str = "") -> str:
        if suite and suite in self.suites:
            return self.suites[suite].project
        for p in self.projects.values():
            if (target and target in p.qa_targets) or (suite and suite in p.qa_suites):
                return p.id
        return ""

    def suite_for_run(self, *, target: str, profile: str) -> Optional[SuiteSpec]:
        for s in self.suites.values():
            if (not s.targets or target in s.targets) and (not s.profiles or profile in s.profiles) and (s.targets or s.profiles):
                return s
        return None

    def project_for_asset(self, *names: str) -> str:
        for p in self.projects.values():
            for pat in p.sentinel_assets:
                if any(n and fnmatchcase(str(n), pat) for n in names):
                    return p.id
        return ""

    def project_for_mission(self, *names: str) -> str:
        for p in self.projects.values():
            for pat in p.missions:
                if any(n and fnmatchcase(str(n).lower(), pat.lower()) for n in names):
                    return p.id
        return ""

    def project_for_product(self, product_id: str) -> str:
        for p in self.projects.values():
            if product_id in p.products:
                return p.id
        return ""

    def source_project(self, source_id: str) -> str:
        return str((self.sources.get(source_id) or {}).get("project") or "")


def _closed(d: Mapping[str, Any], allowed: set, what: str) -> None:
    extra = sorted(set(d) - allowed)
    if extra:
        raise ConfigError(f"{what}: unknown keys {extra}")


def _ids(v: Any, what: str, *, pattern: bool = False) -> Tuple[str, ...]:
    if v in (None, ""):
        return ()
    if not isinstance(v, list) or not all(isinstance(x, str) and x and len(x) <= 120 for x in v):
        raise ConfigError(f"{what}: must be a list of short strings")
    if not pattern and not all(_ID.match(x) for x in v):
        raise ConfigError(f"{what}: ids may hold letters, digits and . _ : - only")
    return tuple(v)


def _str(v: Any, what: str, default: str = "", limit: int = 120) -> str:
    if v in (None, ""):
        return default
    if not isinstance(v, str) or len(v) > limit or any(ord(c) < 32 for c in v):
        raise ConfigError(f"{what}: must be a short string")
    return v.strip()


def _grant(raw: Any, what: str) -> Grant:
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{what}: must be a mapping")
    _closed(raw, _GRANT, what)

    def one(key: str) -> Optional[Tuple[str, ...]]:
        v = raw.get(key, [])
        return None if v == "*" else _ids(v, f"{what}.{key}")
    return Grant(projects=one("projects"), sources=one("sources"))


def parse_config(doc: Any, *, source: str = "") -> DashboardsConfig:
    cfg = DashboardsConfig(source=source)
    if not isinstance(doc, Mapping):
        cfg.errors.append("dashboards file is not a mapping")
        return cfg
    try:
        _closed(doc, _TOP, "dashboards")
        if doc.get("schema") != SCHEMA:
            raise ConfigError(f"schema must be {SCHEMA}")
        cfg.data_dir = _str(doc.get("data_dir"), "data_dir", limit=400)
    except ConfigError as e:
        cfg.errors.append(str(e))
        return cfg

    for raw in doc.get("projects") or []:
        try:
            if not isinstance(raw, Mapping):
                raise ConfigError("project entry is not a mapping")
            _closed(raw, _PROJECT, f"project {raw.get('id', '?')}")
            pid = raw.get("id")
            if not isinstance(pid, str) or not _ID.match(pid):
                raise ConfigError("project id is missing or malformed")
            if pid in cfg.projects:
                raise ConfigError(f"duplicate project {pid}")
            m = raw.get("match") or {}
            if not isinstance(m, Mapping):
                raise ConfigError(f"project {pid}: match must be a mapping")
            _closed(m, _MATCH, f"project {pid}: match")
            cfg.projects[pid] = ProjectSpec(
                id=pid, name=_str(raw.get("name"), f"project {pid}: name", pid), product=_str(raw.get("product"), f"project {pid}: product"),
                environment=_str(raw.get("environment"), f"project {pid}: environment"),
                qa_targets=_ids(m.get("qa_targets"), f"project {pid}: qa_targets"),
                qa_suites=_ids(m.get("qa_suites"), f"project {pid}: qa_suites"),
                sentinel_assets=_ids(m.get("sentinel_assets"), f"project {pid}: sentinel_assets", pattern=True),
                missions=_ids(m.get("missions"), f"project {pid}: missions", pattern=True),
                products=_ids(m.get("products"), f"project {pid}: products"))
        except ConfigError as e:
            cfg.errors.append(str(e))

    srcs = doc.get("sources") or {}
    if not isinstance(srcs, Mapping):
        cfg.errors.append("sources must be a mapping")
        srcs = {}
    for sid, opts in srcs.items():
        if sid not in BUSINESS_SOURCES:
            cfg.errors.append(f"sources: unknown source {sid!r} (known: {', '.join(BUSINESS_SOURCES)})")
            continue
        opts = opts or {}
        if not isinstance(opts, Mapping):
            cfg.errors.append(f"sources.{sid}: must be a mapping")
            continue
        proj = opts.get("project") or ""
        if proj and proj not in cfg.projects:
            cfg.errors.append(f"sources.{sid}: project {proj!r} is not declared")
            continue
        cfg.sources[sid] = dict(opts)

    qa = doc.get("qa") or {}
    if not isinstance(qa, Mapping):
        cfg.errors.append("qa must be a mapping")
        qa = {}
    days = qa.get("inventory_days", 30)
    cfg.inventory_days = days if isinstance(days, int) and not isinstance(days, bool) and 1 <= days <= 365 else 30
    for raw in qa.get("requirements") or []:
        try:
            if not isinstance(raw, Mapping):
                raise ConfigError("requirement entry is not a mapping")
            _closed(raw, _REQ, f"requirement {raw.get('id', '?')}")
            rid = raw.get("id")
            if not isinstance(rid, str) or not _ID.match(rid) or rid in cfg.requirements:
                raise ConfigError(f"requirement id {rid!r} is malformed or duplicated")
            cfg.requirements[rid] = RequirementSpec(id=rid, title=_str(raw.get("title"), f"requirement {rid}: title", rid, 200),
                                                    project=_str(raw.get("project"), f"requirement {rid}: project"),
                                                    priority=_str(raw.get("priority"), f"requirement {rid}: priority"))
        except ConfigError as e:
            cfg.errors.append(str(e))
    for raw in qa.get("suites") or []:
        try:
            if not isinstance(raw, Mapping):
                raise ConfigError("suite entry is not a mapping")
            _closed(raw, _SUITE, f"suite {raw.get('id', '?')}")
            sid = raw.get("id")
            if not isinstance(sid, str) or not _ID.match(sid) or sid in cfg.suites:
                raise ConfigError(f"suite id {sid!r} is malformed or duplicated")
            proj = raw.get("project")
            if proj not in cfg.projects:
                raise ConfigError(f"suite {sid}: project {proj!r} is not declared")
            tests = []
            for t in raw.get("tests") or []:
                if not isinstance(t, Mapping):
                    raise ConfigError(f"suite {sid}: a test entry is not a mapping")
                _closed(t, _TEST, f"suite {sid}: test {t.get('id', '?')}")
                tid = t.get("id")
                if not isinstance(tid, str) or not tid or len(tid) > 300:
                    raise ConfigError(f"suite {sid}: a test id is missing")
                tests.append(TestSpec(id=tid, title=_str(t.get("title"), f"suite {sid}: test title", limit=300),
                                      requirements=_ids(t.get("requirements"), f"suite {sid}: test {tid}: requirements"),
                                      platforms=_ids(t.get("platforms"), f"suite {sid}: test {tid}: platforms")))
            cfg.suites[sid] = SuiteSpec(
                id=sid, project=proj, name=_str(raw.get("name"), f"suite {sid}: name", sid),
                environment=_str(raw.get("environment"), f"suite {sid}: environment"),
                product=_str(raw.get("product"), f"suite {sid}: product"),
                platforms=_ids(raw.get("platforms"), f"suite {sid}: platforms"),
                targets=_ids(raw.get("targets"), f"suite {sid}: targets"),
                profiles=_ids(raw.get("profiles"), f"suite {sid}: profiles"), tests=tuple(tests))
        except ConfigError as e:
            cfg.errors.append(str(e))

    if "access" in doc:
        cfg.access_configured = True             # from here on, anything not granted is refused
        acc = doc.get("access") or {}
        try:
            if not isinstance(acc, Mapping):
                raise ConfigError("access must be a mapping")
            _closed(acc, {"anonymous", "principals"}, "access")
            if "anonymous" in acc:
                cfg.anonymous = _grant(acc["anonymous"], "access.anonymous")
            pr = acc.get("principals") or {}
            if not isinstance(pr, Mapping):
                raise ConfigError("access.principals must be a mapping")
            for name, g in pr.items():
                if not isinstance(name, str) or not re.match(r"^[A-Za-z0-9._:@-]{1,120}$", name):
                    raise ConfigError("access.principals: a principal name is malformed")
                cfg.principals[name] = _grant(g, f"access.principals.{name}")
        except ConfigError as e:
            cfg.errors.append(str(e))
            cfg.anonymous, cfg.principals = NONE, {}      # a broken access section grants nothing
    return cfg


def load_config(path: "str | Path") -> DashboardsConfig:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        cfg = DashboardsConfig(source=str(p), access_configured=True)       # unreadable file: grant nothing
        cfg.errors.append(f"cannot read dashboards file: {e.strerror or e}")
        return cfg
    try:
        if p.suffix.lower() == ".json":
            doc = json.loads(text)
        else:
            import yaml  # noqa: PLC0415
            doc = yaml.safe_load(text)
    except Exception as e:  # noqa: BLE001
        cfg = DashboardsConfig(source=str(p), access_configured=True)
        cfg.errors.append(f"dashboards file does not parse: {type(e).__name__}")
        return cfg
    return parse_config(doc, source=str(p))


def config_from_env() -> DashboardsConfig:
    path = os.environ.get(ENV, "").strip()
    return load_config(path) if path else DashboardsConfig()


__all__ = ["SCHEMA", "ENV", "BUSINESS_SOURCES", "ConfigError", "ProjectSpec", "TestSpec", "SuiteSpec", "RequirementSpec",
           "Grant", "ALL", "NONE", "DashboardsConfig", "parse_config", "load_config", "config_from_env"]
