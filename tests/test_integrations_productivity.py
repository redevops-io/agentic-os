"""W0 contracts for the Documents / Productivity capability plane."""
from __future__ import annotations

from agentic_os.integrations.manifest import Support
from agentic_os.integrations.productivity import (
    FILE_LIVE_CAPABILITIES,
    GOOGLE_CONTEXT_READER,
    GOOGLE_LIVE_CAPABILITIES,
    GOOGLE_MINIMAL_EDITOR,
    LIBREOFFICE_LIVE_CAPABILITIES,
    MICROSOFT_LIVE_CAPABILITIES,
    PRODUCTIVITY_CATALOG,
    CapabilityGrant,
    DocCapability,
    PhysicalStrategy,
    ProductivityProvider,
    ProviderRole,
    ProviderStatus,
    default_registry,
    file_provider,
    google_provider,
    libreoffice_provider,
    microsoft_provider,
)


def test_physical_strategy_is_four_way_with_locality():
    assert PhysicalStrategy.CLOUD_API.is_os_independent
    assert PhysicalStrategy.FILE_NATIVE.is_os_independent
    # the two local strategies need a local connector and are NOT os-independent
    for s in (PhysicalStrategy.LOCAL_HEADLESS, PhysicalStrategy.LOCAL_DESKTOP_AUTOMATION):
        assert s.is_local and s.requires_local_connector and not s.is_os_independent
    assert not PhysicalStrategy.CLOUD_API.requires_local_connector


def test_logical_surface_families_and_mutation():
    assert DocCapability.SHEET_WRITE.family == "sheet" and DocCapability.SHEET_WRITE.verb == "write"
    # writes need a governed envelope; reads / calculate / render do not mutate the source
    assert DocCapability.DOCUMENT_EDIT.mutating and DocCapability.SHEET_WRITE.mutating
    assert not DocCapability.SHEET_READ.mutating
    assert not DocCapability.SHEET_CALCULATE.mutating and not DocCapability.SLIDES_RENDER.mutating


def test_google_scope_profiles_are_least_privilege():
    # drive.file is intentionally narrow — cannot read an existing corpus …
    assert not GOOGLE_MINIMAL_EDITOR.corpus_wide
    assert GOOGLE_MINIMAL_EDITOR.scopes == ("https://www.googleapis.com/auth/drive.file",)
    # … so a Source connect needs the read-only, corpus-wide reader profile
    assert GOOGLE_CONTEXT_READER.read_only and GOOGLE_CONTEXT_READER.corpus_wide


def test_capability_grant_carries_a_ref_never_a_token():
    grant = CapabilityGrant(provider="google", capability="sheet.write",
                            credential_ref="google:token#acct1", scope_profile="GOOGLE_SHEETS_EDITOR")
    fields = set(grant.__dataclass_fields__)
    # owner #3 gets a reference only — no token / secret field exists on the grant
    assert "credential_ref" in fields
    assert not (fields & {"token", "access_token", "client_secret", "secret"})
    assert grant.role is ProviderRole.APP


def test_catalog_has_the_six_providers_in_priority_order():
    order = [p.provider for p in PRODUCTIVITY_CATALOG]
    assert order == ["google", "microsoft", "file", "libreoffice", "office_desktop", "iwork"]
    google = PRODUCTIVITY_CATALOG[0]
    # google is both an APP and a SOURCE, cloud, with the four scope profiles
    assert google.has_role(ProviderRole.APP) and google.has_role(ProviderRole.SOURCE)
    assert google.strategy is PhysicalStrategy.CLOUD_API
    assert {p.name for p in google.scope_profiles} == {
        "GOOGLE_MINIMAL_EDITOR", "GOOGLE_CONTEXT_READER", "GOOGLE_DOCS_EDITOR", "GOOGLE_SHEETS_EDITOR"}


def test_per_capability_strategy_override():
    office = next(p for p in PRODUCTIVITY_CATALOG if p.provider == "office_desktop")
    # the suite's primary is desktop automation …
    assert office.strategy is PhysicalStrategy.LOCAL_DESKTOP_AUTOMATION
    # … but a pure file edit resolves to FILE_NATIVE (no need to drive the running app)
    assert office.strategy_for("document.edit") is PhysicalStrategy.FILE_NATIVE
    assert office.strategy_for("slides.render") is PhysicalStrategy.LOCAL_DESKTOP_AUTOMATION


def test_registry_queries_by_capability_and_source_kind():
    reg = default_registry()
    # everyone offers sheet.write as an APP; only the two clouds are SOURCEs of mail-ish kinds
    writers = {p.provider for p in reg.for_capability("sheet.write", ProviderRole.APP)}
    assert {"google", "microsoft", "file", "libreoffice", "office_desktop", "iwork"} <= writers
    assert {p.provider for p in reg.sources_of("drive")} == {"google"}
    assert {p.provider for p in reg.sources_of("sharepoint")} == {"microsoft"}


def test_manifest_projection_reality_filter():
    reg = default_registry()
    m = reg.to_manifest()
    # W1+W2+W3+W4: google, microsoft, file AND libreoffice are LIVE for the caps their adapters
    # implement → buildable. libreoffice (LOCAL_HEADLESS) adds sheet.write + document.create …
    assert set(m.buildable("sheet.write")) == {"google", "microsoft", "file", "libreoffice"}
    assert set(m.buildable("sheet.read")) == {"google", "microsoft", "file"}   # libreoffice sheet.read = UNO (planned)
    assert set(m.buildable("document.create")) == {"google", "microsoft", "file", "libreoffice"}
    # W3 flips document.read live for file (the cloud adapters do not implement it)
    assert set(m.buildable("document.read")) == {"file"}
    # W4 is the ONLY provider that renders slides → PDF live (LOCAL_HEADLESS export)
    assert set(m.buildable("slides.render")) == {"libreoffice"}
    assert m.decide("sheet.write", "google") is None            # runnable
    assert m.decide("sheet.write", "microsoft") is None         # runnable (W2)
    assert m.decide("sheet.write", "file") is None              # runnable (W3)
    assert m.decide("sheet.write", "libreoffice") is None       # runnable (W4)
    assert m.decide("slides.render", "libreoffice") is None     # runnable (W4)
    # … but the un-implemented caps stay planned (partial liveness), for every live provider
    assert m.buildable("document.edit") == ()                   # planned for everyone
    assert m.buildable("slides.create") == ()                   # UNO — planned even for libreoffice
    assert m.buildable("slides.read") == ()
    assert m.decide("document.edit", "google") is not None       # planned → refused by name
    assert m.decide("document.edit", "microsoft") is not None    # planned → refused by name
    assert m.decide("document.edit", "file") is not None         # planned → refused by name (W3)
    assert m.decide("document.edit", "libreoffice") is not None  # planned → refused by name (W4, UNO)
    assert m.decide("slides.create", "libreoffice") is not None  # planned → refused by name (W4, UNO)


def test_w1_google_is_partly_live_with_both_roles_and_scope_profiles():
    g = google_provider()
    # W1 flips google LIVE, but only for the capabilities its adapter implements
    assert g.status is ProviderStatus.LIVE
    assert set(GOOGLE_LIVE_CAPABILITIES) == {"sheet.read", "sheet.write", "document.create"}
    for cap in GOOGLE_LIVE_CAPABILITIES:
        assert g.is_capability_live(cap)
    # document.edit + slides.* stay planned (partial liveness)
    for cap in ("document.edit", "slides.create", "slides.render", "slides.read"):
        assert not g.is_capability_live(cap)
    # both roles survive: APP (the new adapter) + SOURCE (the existing Drive connector) …
    assert g.has_role(ProviderRole.APP) and g.has_role(ProviderRole.SOURCE)
    assert "drive" in g.source_kinds
    # … and the four GOOGLE_* scope profiles are carried
    assert {p.name for p in g.scope_profiles} == {
        "GOOGLE_MINIMAL_EDITOR", "GOOGLE_CONTEXT_READER", "GOOGLE_DOCS_EDITOR", "GOOGLE_SHEETS_EDITOR"}
    # the registry still exposes google as a Source of drive
    assert {p.provider for p in default_registry().sources_of("drive")} == {"google"}


def test_w2_microsoft_is_partly_live_app_but_source_is_not_live():
    m = microsoft_provider()
    # W2 flips microsoft LIVE, but only for the capabilities its adapter implements
    assert m.status is ProviderStatus.LIVE
    assert set(MICROSOFT_LIVE_CAPABILITIES) == {"sheet.read", "sheet.write", "document.create"}
    for cap in MICROSOFT_LIVE_CAPABILITIES:
        assert m.is_capability_live(cap)
    # document.edit + slides.* stay planned (partial liveness)
    for cap in ("document.edit", "slides.create", "slides.render", "slides.read"):
        assert not m.is_capability_live(cap)
    # both roles are declared — APP (the new adapter) + SOURCE — over the MS source kinds …
    assert m.has_role(ProviderRole.APP) and m.has_role(ProviderRole.SOURCE)
    assert set(m.source_kinds) == {"onedrive", "sharepoint", "outlook", "teams"}
    # … but the SOURCE side is NOT wired: there is no OneDrive/SharePoint Source connector yet, so
    # the manifest (APP-only projection) is what carries microsoft's liveness, buildable by name.
    manifest = default_registry().to_manifest()
    assert "microsoft" in manifest.buildable("sheet.write")
    assert "microsoft" in manifest.buildable("document.create")


def test_manifest_now_makes_sheet_write_buildable_by_google_and_microsoft():
    m = default_registry().to_manifest()
    assert set(m.buildable("sheet.write")) == {"google", "microsoft", "file", "libreoffice"}
    assert m.decide("sheet.write", "microsoft") is None      # runnable (W2)
    assert m.decide("sheet.write", "file") is None           # runnable (W3)
    assert m.decide("sheet.write", "libreoffice") is None    # runnable (W4)


def test_w3_file_is_partly_live_local_no_auth():
    f = file_provider()
    # W3 flips file LIVE, but only for the capabilities its stdlib adapter implements
    assert f.status is ProviderStatus.LIVE
    assert set(FILE_LIVE_CAPABILITIES) == {
        "sheet.read", "sheet.write", "document.read", "document.create"}
    for cap in FILE_LIVE_CAPABILITIES:
        assert f.is_capability_live(cap)
    # document.edit + slides.* stay planned (partial liveness)
    for cap in ("document.edit", "slides.create", "slides.read"):
        assert not f.is_capability_live(cap)
    # file is a LOCAL, FILE_NATIVE, APP-only provider (no SOURCE role, no cloud, no auth)
    assert f.strategy is PhysicalStrategy.FILE_NATIVE
    assert f.has_role(ProviderRole.APP) and not f.has_role(ProviderRole.SOURCE)
    assert f.scope_profiles == ()


def test_w4_libreoffice_is_partly_live_local_headless_no_auth():
    lo = libreoffice_provider()
    # W4 flips libreoffice LIVE, but only for the caps its headless-conversion adapter implements —
    # exactly the authoring gaps W3's `file` deferred (real XLSX write, DOCX/PDF create) + a PDF render
    assert lo.status is ProviderStatus.LIVE
    assert set(LIBREOFFICE_LIVE_CAPABILITIES) == {"sheet.write", "document.create", "slides.render"}
    for cap in LIBREOFFICE_LIVE_CAPABILITIES:
        assert lo.is_capability_live(cap)
    # the UNO-bridge caps stay planned (partial liveness): document.edit, cell-level sheet editing
    # (sheet.read here is UNO-only), slides.create/slides.read
    for cap in ("document.edit", "sheet.read", "slides.create", "slides.read"):
        assert not lo.is_capability_live(cap)
    # libreoffice is a LOCAL, LOCAL_HEADLESS, APP-only provider (no SOURCE role, no cloud, no auth)
    assert lo.strategy is PhysicalStrategy.LOCAL_HEADLESS
    assert lo.strategy.requires_local_connector
    assert lo.has_role(ProviderRole.APP) and not lo.has_role(ProviderRole.SOURCE)
    assert lo.scope_profiles == ()
    # slides.render is buildable by libreoffice ALONE (the only live PDF-render path)
    assert set(default_registry().to_manifest().buildable("slides.render")) == {"libreoffice"}
