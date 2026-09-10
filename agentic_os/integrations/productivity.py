"""Documents / Productivity capability plane — contracts (W0).

The deterministic seam the office/productivity providers (Google Workspace, Microsoft 365,
LibreOffice, desktop Office, Apple iWork, raw file formats) implement against. See
``PRODUCTIVITY_PLANE.md`` for the design. This module is pure and model-free — no I/O, no
provider SDK — additive to the Integration Plane: it reuses the plane's manifest
(:class:`Support` / :class:`CapabilityDimension` / :class:`IntegrationManifest`) and only adds
what documents need on top:

* a provider-independent **logical capability surface** (:class:`DocCapability`),
* the four **physical execution strategies** a planner resolves between (:class:`PhysicalStrategy`),
* least-privilege **scope profiles** requested per intended use (:class:`ScopeProfile`),
* a **provider descriptor** carrying a suite's APP and/or SOURCE roles (:class:`ProductivityProvider`),
* a **catalog** of the six providers × strategies (planned/live) + a registry that projects it into
  the reality-filter :class:`IntegrationManifest`, and
* a :class:`CapabilityGrant` — what a Mission actually receives: scoped authority *by reference*,
  never the end-user token and never the OAuth client secret.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Mapping, Optional, Tuple

from .manifest import CapabilityDimension, IntegrationManifest, Support


# ── Physical execution strategy — a 4-way resolution, not "cloud vs desktop" ────────────
class PhysicalStrategy(str, Enum):
    """How a logical capability is physically fulfilled. The planner resolves one per
    *operation* (e.g. "edit cell B14" → FILE_NATIVE; "recalc exactly as Excel" → CLOUD_API)."""

    CLOUD_API = "CLOUD_API"                                  # provider REST/Graph API
    FILE_NATIVE = "FILE_NATIVE"                              # parse/edit the file format directly
    LOCAL_HEADLESS = "LOCAL_HEADLESS"                        # headless office engine on the box
    LOCAL_DESKTOP_AUTOMATION = "LOCAL_DESKTOP_AUTOMATION"    # drive the running desktop app

    @property
    def is_local(self) -> bool:
        return self in (PhysicalStrategy.LOCAL_HEADLESS, PhysicalStrategy.LOCAL_DESKTOP_AUTOMATION)

    @property
    def requires_local_connector(self) -> bool:
        """LOCAL_* strategies run on the user's machine (a local connector), never hosted."""
        return self.is_local

    @property
    def is_os_independent(self) -> bool:
        return self in (PhysicalStrategy.CLOUD_API, PhysicalStrategy.FILE_NATIVE)


# ── The two sides of the Apps ⇄ Sources boundary a suite can occupy ─────────────────────
class ProviderRole(str, Enum):
    """A suite is usually BOTH — but Connecting as an APP must not imply ingesting as a SOURCE."""

    APP = "APP"        # ACTION path: create/update/send, under a GovernedEnvelope
    SOURCE = "SOURCE"  # EVIDENCE path: index scoped content → EvidenceRef


# ── The provider-independent logical capability surface ─────────────────────────────────
class DocCapability(str, Enum):
    """The verbs, regardless of who fulfils them. Ids namespace under document/sheet/slides."""

    DOCUMENT_READ = "document.read"
    DOCUMENT_CREATE = "document.create"
    DOCUMENT_EDIT = "document.edit"
    SHEET_READ = "sheet.read"
    SHEET_WRITE = "sheet.write"
    SHEET_CALCULATE = "sheet.calculate"
    SLIDES_READ = "slides.read"
    SLIDES_CREATE = "slides.create"
    SLIDES_RENDER = "slides.render"

    @property
    def family(self) -> str:
        return self.value.split(".", 1)[0]          # document | sheet | slides

    @property
    def verb(self) -> str:
        return self.value.split(".", 1)[1]

    @property
    def mutating(self) -> bool:
        """A write op (needs a GovernedEnvelope). read/calculate/render do not mutate the source."""
        return self.verb in ("create", "edit", "write")


#: All logical capabilities, grouped by family — handy for provider declarations.
DOCUMENT_CAPABILITIES: Tuple[str, ...] = tuple(c.value for c in DocCapability if c.family == "document")
SHEET_CAPABILITIES: Tuple[str, ...] = tuple(c.value for c in DocCapability if c.family == "sheet")
SLIDES_CAPABILITIES: Tuple[str, ...] = tuple(c.value for c in DocCapability if c.family == "slides")


# ── Least-privilege scope profiles (requested per intended use) ─────────────────────────
@dataclass(frozen=True)
class ScopeProfile:
    """A named, least-privilege set of provider scopes. A ProviderConnectDescriptor requests
    ONE profile — the narrowness (e.g. Google ``drive.file``: only app-created/opened files)
    is a property of the profile, not the provider. ``corpus_wide`` is the load-bearing flag:
    a minimal editor profile cannot read an existing corpus, so a SOURCE connect needs a
    reader profile."""

    name: str
    provider: str
    scopes: Tuple[str, ...]
    read_only: bool = False
    corpus_wide: bool = False    # can it reach content the app did not itself create?
    note: str = ""


# ── What a Mission receives: scoped authority BY REFERENCE (owner #3) ────────────────────
@dataclass(frozen=True)
class CapabilityGrant:
    """The runtime-scoped authority a Mission is handed. It carries a ``credential_ref`` into
    the CredentialBroker — **never** the end-user access token (owner #2, stays in the broker)
    and **never** the OAuth client secret (owner #1, stays in the connect/deployment layer).
    So the model / Sidekick can plan against a capability without ever seeing a secret."""

    provider: str
    capability: str
    credential_ref: str          # opaque broker ref — resolved at execution, out of the model's sight
    scope_profile: str = ""
    role: ProviderRole = ProviderRole.APP


# ── Provider descriptor: a suite's roles, capabilities, strategy and scope profiles ─────
class ProviderStatus(str, Enum):
    PLANNED = "planned"          # in the catalog/roadmap; no live adapter yet
    LIVE = "live"                # a live adapter/connector is wired (a real Connect works)


@dataclass(frozen=True)
class ProductivityProvider:
    """A productivity suite as a plane provider — descriptor only; adapters (APP) and source
    connectors (SOURCE) are supplied by the live provider modules (W1+)."""

    provider: str
    display_name: str
    roles: Tuple[ProviderRole, ...]
    strategy: PhysicalStrategy                                  # the suite's primary physical family
    app_capabilities: Tuple[str, ...] = ()                     # DocCapability values offered as an APP
    source_kinds: Tuple[str, ...] = ()                         # what it can be a SOURCE of ('drive', 'onedrive', …)
    scope_profiles: Tuple[ScopeProfile, ...] = ()
    status: ProviderStatus = ProviderStatus.PLANNED
    per_capability_strategy: Mapping[str, PhysicalStrategy] = field(default_factory=dict)
    #: The capabilities that are actually LIVE (a real adapter fulfils them), when a provider is
    #: only *partly* live — W1 Google is LIVE for sheet.read/sheet.write/document.create but its
    #: document.edit + slides.* stay planned. Empty ⇒ the provider-level ``status`` governs every
    #: capability uniformly (a fully-planned or fully-live suite).
    live_capabilities: Tuple[str, ...] = ()

    def is_capability_live(self, capability: str) -> bool:
        """Whether one capability is backed by a live adapter. With ``live_capabilities`` set,
        only those are live (partial liveness); otherwise the provider-level status decides."""
        if self.live_capabilities:
            return capability in self.live_capabilities
        return self.status is ProviderStatus.LIVE

    def has_role(self, role: ProviderRole) -> bool:
        return role in self.roles

    def offers(self, capability: str) -> bool:
        return capability in self.app_capabilities

    def strategy_for(self, capability: str) -> PhysicalStrategy:
        """The physical strategy for a capability — a per-capability override, else the suite's
        primary. (The planner may still pick a different strategy for a specific operation's
        requirement; this is the provider's declared default.)"""
        return self.per_capability_strategy.get(capability, self.strategy)

    def profile(self, name: str) -> Optional[ScopeProfile]:
        for p in self.scope_profiles:
            if p.name == name:
                return p
        return None


# ── Registry over the provider catalog ──────────────────────────────────────────────────
@dataclass
class ProductivityRegistry:
    """The known productivity providers, queried by capability/role/strategy, and projected
    into the reality-filter :class:`IntegrationManifest` (LIVE ⇒ EXECUTED, PLANNED ⇒ NOT_MODELLED)."""

    _by_provider: Dict[str, ProductivityProvider] = field(default_factory=dict)

    def register(self, provider: ProductivityProvider) -> "ProductivityRegistry":
        self._by_provider[provider.provider] = provider
        return self

    def get(self, provider: str) -> Optional[ProductivityProvider]:
        return self._by_provider.get(provider)

    def providers(self) -> Tuple[ProductivityProvider, ...]:
        return tuple(self._by_provider[k] for k in sorted(self._by_provider))

    def for_capability(self, capability: str, role: ProviderRole = ProviderRole.APP) -> Tuple[ProductivityProvider, ...]:
        return tuple(p for p in self.providers() if p.has_role(role) and p.offers(capability))

    def sources_of(self, source_kind: str) -> Tuple[ProductivityProvider, ...]:
        return tuple(p for p in self.providers()
                     if p.has_role(ProviderRole.SOURCE) and source_kind in p.source_kinds)

    def to_manifest(self) -> IntegrationManifest:
        """Reality filter: one CapabilityDimension per (capability, provider) an APP offers —
        EXECUTED where the provider is LIVE, NOT_MODELLED where it is only PLANNED — so the
        wizard's grounding/refusal logic works over productivity providers unchanged."""
        dims = []
        for p in self.providers():
            if not p.has_role(ProviderRole.APP):
                continue
            for cap in p.app_capabilities:
                live = p.is_capability_live(cap)          # per-capability (supports partial liveness)
                support = Support.EXECUTED if live else Support.NOT_MODELLED
                why = "" if live else f"{p.display_name} adapter not wired yet (planned)"
                dims.append(CapabilityDimension(capability=cap, provider=p.provider,
                                                support=support, tier=_TIER.get(cap, 0), why=why))
        return IntegrationManifest(dimensions=tuple(dims))


#: Coarse risk tiers for the logical caps (read=0, create/edit=2, render/calculate=1). Adjustable.
_TIER: Dict[str, int] = {
    **{c: 0 for c in (DocCapability.DOCUMENT_READ, DocCapability.SHEET_READ, DocCapability.SLIDES_READ)},
    **{c: 1 for c in (DocCapability.SHEET_CALCULATE, DocCapability.SLIDES_RENDER)},
    **{c: 2 for c in (DocCapability.DOCUMENT_CREATE, DocCapability.DOCUMENT_EDIT,
                      DocCapability.SHEET_WRITE, DocCapability.SLIDES_CREATE)},
}
_TIER = {k.value if isinstance(k, DocCapability) else k: v for k, v in _TIER.items()}


# ── Google scope profiles (real scope URLs; W1 flips google to LIVE and wires the adapter) ──
_G = "https://www.googleapis.com/auth/"
GOOGLE_MINIMAL_EDITOR = ScopeProfile("GOOGLE_MINIMAL_EDITOR", "google", (_G + "drive.file",),
                                     corpus_wide=False, note="only files the app created or the user opened")
GOOGLE_CONTEXT_READER = ScopeProfile("GOOGLE_CONTEXT_READER", "google", (_G + "drive.readonly",),
                                     read_only=True, corpus_wide=True, note="index an existing corpus as a Source")
GOOGLE_DOCS_EDITOR = ScopeProfile("GOOGLE_DOCS_EDITOR", "google", (_G + "drive.file", _G + "documents"))
GOOGLE_SHEETS_EDITOR = ScopeProfile("GOOGLE_SHEETS_EDITOR", "google", (_G + "drive.file", _G + "spreadsheets"))
GOOGLE_SCOPE_PROFILES: Tuple[ScopeProfile, ...] = (
    GOOGLE_MINIMAL_EDITOR, GOOGLE_CONTEXT_READER, GOOGLE_DOCS_EDITOR, GOOGLE_SHEETS_EDITOR)


# ── The catalog: the six providers × strategies, in priority order. Mostly PLANNED at W0;
#    W1 flips 'google' to LIVE. Physical strategy is per-suite, with the documented overrides. ──
_ALL_DOC_CAPS: Tuple[str, ...] = tuple(c.value for c in DocCapability)

#: The logical capabilities the W1 Google App adapter (``agentic_os.integrations.google_app``)
#: actually implements live. document.edit + the slides.* family remain planned (a later wave).
GOOGLE_LIVE_CAPABILITIES: Tuple[str, ...] = (
    DocCapability.SHEET_READ.value,
    DocCapability.SHEET_WRITE.value,
    DocCapability.DOCUMENT_CREATE.value,
)

#: The logical capabilities the W2 Microsoft App adapter (``agentic_os.integrations.microsoft_app``)
#: actually implements live — the same three as Google (Excel workbook read/write + a OneDrive file
#: create). document.edit + the slides.* family remain planned (a later wave).
MICROSOFT_LIVE_CAPABILITIES: Tuple[str, ...] = (
    DocCapability.SHEET_READ.value,
    DocCapability.SHEET_WRITE.value,
    DocCapability.DOCUMENT_CREATE.value,
)

PRODUCTIVITY_CATALOG: Tuple[ProductivityProvider, ...] = (
    ProductivityProvider(
        provider="google", display_name="Google Workspace",
        roles=(ProviderRole.APP, ProviderRole.SOURCE), strategy=PhysicalStrategy.CLOUD_API,
        app_capabilities=_ALL_DOC_CAPS, source_kinds=("drive", "gmail", "calendar"),
        scope_profiles=GOOGLE_SCOPE_PROFILES,
        # W1: google is (partly) LIVE — sheet.read/sheet.write/document.create are wired.
        status=ProviderStatus.LIVE, live_capabilities=GOOGLE_LIVE_CAPABILITIES),
    ProductivityProvider(
        provider="microsoft", display_name="Microsoft 365 (Graph)",
        roles=(ProviderRole.APP, ProviderRole.SOURCE), strategy=PhysicalStrategy.CLOUD_API,
        app_capabilities=_ALL_DOC_CAPS, source_kinds=("onedrive", "sharepoint", "outlook", "teams"),
        # W2: microsoft is (partly) LIVE as an APP — sheet.read/sheet.write/document.create are wired
        # by microsoft_app.MicrosoftWorkbookDocsAdapter. Its SOURCE role is declared (source_kinds
        # above) but NOT live: there is no OneDrive/SharePoint Source connector yet (unlike Google's
        # sources_drive.py) — a follow-on. to_manifest() only projects the APP role, so declaring the
        # SOURCE role here does not claim SOURCE liveness.
        status=ProviderStatus.LIVE, live_capabilities=MICROSOFT_LIVE_CAPABILITIES),
    ProductivityProvider(
        provider="file", display_name="File formats (Open XML / ODF / PDF / CSV / MD)",
        roles=(ProviderRole.APP,), strategy=PhysicalStrategy.FILE_NATIVE,
        app_capabilities=(DocCapability.DOCUMENT_READ.value, DocCapability.DOCUMENT_CREATE.value,
                          DocCapability.DOCUMENT_EDIT.value, DocCapability.SHEET_READ.value,
                          DocCapability.SHEET_WRITE.value, DocCapability.SLIDES_READ.value,
                          DocCapability.SLIDES_CREATE.value),
        status=ProviderStatus.PLANNED),
    ProductivityProvider(
        provider="libreoffice", display_name="LibreOffice (UNO / headless)",
        roles=(ProviderRole.APP,), strategy=PhysicalStrategy.LOCAL_HEADLESS,
        app_capabilities=_ALL_DOC_CAPS, status=ProviderStatus.PLANNED,
        # exact recalculation / render are the desktop engine's strength
        per_capability_strategy={DocCapability.SLIDES_RENDER.value: PhysicalStrategy.LOCAL_HEADLESS}),
    ProductivityProvider(
        provider="office_desktop", display_name="Microsoft Office (desktop)",
        roles=(ProviderRole.APP,), strategy=PhysicalStrategy.LOCAL_DESKTOP_AUTOMATION,
        app_capabilities=_ALL_DOC_CAPS, status=ProviderStatus.PLANNED,
        # headless Open XML for pure file edits; the running app only when exact behaviour matters
        per_capability_strategy={
            DocCapability.DOCUMENT_EDIT.value: PhysicalStrategy.FILE_NATIVE,
            DocCapability.SHEET_WRITE.value: PhysicalStrategy.FILE_NATIVE}),
    ProductivityProvider(
        provider="iwork", display_name="Apple iWork (Pages / Numbers / Keynote)",
        roles=(ProviderRole.APP,), strategy=PhysicalStrategy.LOCAL_DESKTOP_AUTOMATION,
        app_capabilities=_ALL_DOC_CAPS, status=ProviderStatus.PLANNED),
)


def google_provider() -> ProductivityProvider:
    """Google Workspace as the W1 (partly) LIVE provider — the catalog's ``google`` entry.

    LIVE for ``sheet.read`` / ``sheet.write`` / ``document.create`` (fulfilled by
    :class:`~agentic_os.integrations.google_app.GoogleWorkspaceDocsAdapter`); ``document.edit`` and
    the ``slides.*`` family stay planned. Both roles — APP (the adapter) and SOURCE (the existing
    :class:`~agentic_os.sources_drive.GoogleDriveSourceConnector`) — with the four GOOGLE_* scope
    profiles."""
    return next(p for p in PRODUCTIVITY_CATALOG if p.provider == "google")


def microsoft_provider() -> ProductivityProvider:
    """Microsoft 365 (Graph) as the W2 (partly) LIVE provider — the catalog's ``microsoft`` entry.

    LIVE (as an APP) for ``sheet.read`` / ``sheet.write`` / ``document.create`` (fulfilled by
    :class:`~agentic_os.integrations.microsoft_app.MicrosoftWorkbookDocsAdapter`); ``document.edit``
    and the ``slides.*`` family stay planned. The SOURCE role is declared over
    ``onedrive``/``sharepoint``/``outlook``/``teams`` but is **not** yet live — no OneDrive/SharePoint
    Source connector exists (a follow-on), unlike Google's ``GoogleDriveSourceConnector``."""
    return next(p for p in PRODUCTIVITY_CATALOG if p.provider == "microsoft")


def default_registry() -> ProductivityRegistry:
    """The catalog as a registry (google W1-LIVE + microsoft W2-LIVE for their implemented caps)."""
    reg = ProductivityRegistry()
    for p in PRODUCTIVITY_CATALOG:
        reg.register(p)
    return reg
