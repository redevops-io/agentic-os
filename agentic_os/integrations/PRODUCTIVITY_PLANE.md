# The Documents / Productivity Capability Plane

> One provider-independent documents/productivity capability layer, resolved at runtime to
> cloud APIs, native file manipulation, headless office engines, or local desktop automation.
>
> OAuth **app** secrets belong to the deployment/connect layer, **user access tokens** belong to
> the CredentialBroker, and **Missions receive only scoped capability references**.

This spec extends the Integration Plane (`agentic_os/integrations/`) to the office/productivity
suites people use alongside email and messengers: Google Workspace, Microsoft 365, and the local
desktop editors (Microsoft Office, Apple iWork, LibreOffice), plus raw document/spreadsheet file
formats. It reuses the plane's existing seams — nothing here is a parallel stack.

---

## 1 · Credential trust boundary — three distinct owners

The plane already brokers credentials; the refinement is to name **three owners** and keep each on
its own side of the boundary. This matters for multi-tenant hosted ReDevOps.

```
OAuth client registration      →  deployment / connect layer        (client_id + client_secret)
   ↓  provider consent
End-user access token           →  CredentialBroker                  (per-account identity)
   ↓  authority narrowing
Mission capability              →  runtime, via CapabilityRef only   (scoped authority, derived)
```

- **OAuth client registration** — the app identity, `client_id`+`client_secret`, per provider,
  held in deployment secrets (env). Lives only in the connect layer (`hosted_oauth.py`
  `ProviderOAuthApp` / `_StaticResolver`). Never enters a plan, context, mission or telemetry.
- **End-user access token** — the account identity obtained at the callback; stored in the
  `CredentialBroker` under a `CredentialRef`. This is *not* "the deployment's credential."
- **Mission capability** — runtime-scoped authority **derived** from the token. The Mission — and
  therefore the model / Sidekick — receives **only a capability/credential reference**, never the
  raw end-user token and never the client secret. (Beyond "Mission never sees the client secret":
  the model never sees the *token* either.)

For Microsoft 365 this means **least-privilege delegated permissions** (e.g. `Files.Read` /
`Files.ReadWrite`), requested per intended use — see §4.

---

## 2 · The logical capability surface (provider-independent)

The same verbs regardless of who fulfils them:

```
document.read      sheet.read       slides.read
document.create    sheet.write      slides.create
document.edit      sheet.calculate  slides.render
```

These attach to the existing capability manifest (`manifest.py`) with tiers/authority like any
other capability. Writes execute under a `GovernedEnvelope` (`execution.py`) exactly as today.

---

## 3 · Physical strategy — a 4-way resolution, not "cloud vs desktop"

The planner resolves each *operation* to one of four physical strategies. This is a first-class
enum on the adapter family, and extends the plane's `ActivationCapability{automatable,
human_required, execution_strategy}` idea to documents.

```
CLOUD_API                 provider REST/Graph API              (Google, Microsoft 365)
FILE_NATIVE               parse/edit the file format directly  (Open XML, ODF, PDF, CSV, MD)
LOCAL_HEADLESS            a headless office engine on the box  (soffice --headless, Open XML tools)
LOCAL_DESKTOP_AUTOMATION  drive the running desktop app        (COM, AppleScript/JXA, UNO)
```

Resolution is by *what the operation needs*, not by product:

| Intent | Strategy |
|---|---|
| "Change cell B14 in this .xlsx" | `FILE_NATIVE` (Open XML) |
| "Recalculate the workbook exactly as Excel would" | `CLOUD_API` (Graph Excel) or `LOCAL_DESKTOP_AUTOMATION` |
| "Turn this .docx into PDF" | `LOCAL_HEADLESS` (LibreOffice) or `CLOUD_API` |
| "Modify the currently-open Keynote" | `LOCAL_DESKTOP_AUTOMATION` (JXA) |
| "Index this shared Drive folder as context" | `CLOUD_API` (Drive) → EvidenceRef |

`CLOUD_API` and `FILE_NATIVE` are OS-independent; `LOCAL_HEADLESS` and `LOCAL_DESKTOP_AUTOMATION`
require a **local connector** on the user's machine (the self-host Projects app already runs
there) and are the only strategies that can be human-gated.

---

## 4 · Adapter families

### 4a · Cloud suites (`CLOUD_API`) — same OAuth+adapter flow as Slack/Gmail

**Google Workspace** — OAuth 2.0 + Google APIs. Surfaces: Drive · Docs · Sheets · Slides · Gmail
· Calendar. **Shipped:** `KNOWN_OAUTH` carries a `google` entry (offline-access refresh params) and
`google_app.py` provides the adapter satisfying `AdapterPort`.

Scopes are **use-case profiles** on `ProviderConnectDescriptor`, not one hard-coded set —
`drive.file` is intentionally narrow (only files the app created or the user explicitly opened),
which is right for least privilege but insufficient to read an existing corpus:

```
GOOGLE_MINIMAL_EDITOR   drive.file                         (create/edit app-owned files only)
GOOGLE_CONTEXT_READER   drive.readonly (or metadata.ro)    (index an existing corpus as a Source)
GOOGLE_DOCS_EDITOR      drive.file + documents             (author/edit Docs)
GOOGLE_SHEETS_EDITOR    drive.file + spreadsheets          (author/edit Sheets)
```
The `ProviderConnectDescriptor` requests only the profile the intended Mission needs.

**Microsoft 365** — OAuth 2.0 (Entra ID) + **Microsoft Graph**. The physical model is Graph
sub-surfaces, not "Word/Excel/PowerPoint APIs":

```
Graph
 ├─ Files / OneDrive / SharePoint     (Files.Read, Files.ReadWrite — delegated, least-privilege)
 ├─ Excel workbook API                (strong native API for supported .xlsx)
 ├─ Mail / Outlook
 ├─ Calendar
 └─ Teams                             (history as a Source)
Word / PowerPoint  →  mostly file-centric (Open XML) + a format/tooling strategy
```

### 4b · File-format family (`FILE_NATIVE`) — no live app state required

A distinct family, often preferable to desktop automation: **Open XML** (.docx/.xlsx/.pptx),
**ODF**, **PDF**, **CSV**, **Markdown**. Pure, local, deterministic — good for "edit cell",
"read table", "extract text", "assemble a doc" when exact application recalculation isn't needed.

### 4c · Local desktop family (`LOCAL_HEADLESS` / `LOCAL_DESKTOP_AUTOMATION`)

```
Windows   Microsoft Office desktop   COM / Office Interop; Open XML for headless
macOS     Apple iWork (Pages/Numbers/Keynote)   AppleScript / JXA; direct file where practical
Linux     LibreOffice                UNO; headless soffice; ODF
```
Logical capabilities identical; only the resolved strategy differs. Requires the local connector.

---

## 5 · Apps ⇄ Sources duality (explicit in Projects)

Every suite sits on **both** sides of the Apps/Sources boundary. Connecting a suite as an **App**
must **not** implicitly ingest it as a **Source** — "Connect Google Workspace" ≠ "index all of
Drive". Two separate governed grants.

```
Google Workspace
  as APP     (ACTION path)     create/update Docs·Sheets·Slides · send Gmail · create Calendar event
  as SOURCE  (EVIDENCE path)   retrieve documents · index selected folders → EvidenceRef

Microsoft 365
  as APP                       send Outlook mail · update workbook · create Calendar event
  as SOURCE                    OneDrive · SharePoint · Outlook mail · Teams history → EvidenceRef
```

- APP side → `ProviderConnectDescriptor` → `AdapterPort` (`execute` under `GovernedEnvelope`).
- SOURCE side → `ContextSource` + `SourceConnector` → scoped `EvidenceRef` (the Sources plane),
  with its own `SourceGrant` (folders/labels/sites), `AccessMode`, and `IndexingPolicy`.

---

## 6 · Implementation status

Broadest OS-independent value first, then local execution where cloud APIs can't reach:

1. ✅ **Google Workspace** — Docs/Sheets operations + Drive **as Source**. (`CLOUD_API`) — `google_app.py`
2. ✅ **Microsoft 365** — Excel workbook + OneDrive upload; OneDrive **as Source**. (`CLOUD_API`) — `microsoft_app.py`, `sources_onedrive.py`
3. ✅ **Generic file-format capabilities** — CSV/MD read+write, XLSX/DOCX read. (`FILE_NATIVE`) — `file_formats.py`
4. ✅ **LibreOffice local** — `soffice --convert-to` (CSV→XLSX, text→DOCX/PDF, →PDF). (`LOCAL_HEADLESS`) — `libreoffice_app.py`
5. ⬜ **Windows Office desktop** — only where exact desktop behaviour matters. (`LOCAL_DESKTOP_AUTOMATION`) — not yet shipped
6. ⬜ **Apple iWork** — useful, lowest leverage / most automation-specific. (`LOCAL_DESKTOP_AUTOMATION`) — not yet shipped

Live capabilities are advertised per-provider via `live_capabilities` / `is_capability_live`, so the
manifest reflects what actually runs (partial liveness), not the full catalog.

---

## 7 · How it maps onto existing plane seams (no parallel stack)

| Concept here | Existing seam |
|---|---|
| App connect (OAuth) | `hosted_oauth.py` `KNOWN_OAUTH` + `HostedConnect` / `ProviderConnectDescriptor` |
| Scope profile | field on `ProviderConnectDescriptor` (requested at connect) |
| Provider adapter | `execution.py` `AdapterPort` (`capabilities/connect/execute/observe/health`) |
| Write governance | `GovernedEnvelope` (unchanged) |
| Physical strategy | new `PhysicalStrategy` enum on the adapter family; planner-resolved |
| Source (evidence) | `sources.py` `ContextSource` + `SourceConnector` → `EvidenceRef`, scoped by `SourceGrant` |
| Human-gated op | `ActivationCapability{automatable, human_required, execution_strategy}` |
| Credential refs | client secret in connect layer · token in `CredentialBroker` · Mission gets `CredentialRef` |

**Shipped.** `productivity.py` provides the `PhysicalStrategy` enum, the logical `DocCapability`
surface, `ScopeProfile` (+ `GOOGLE_*` profiles), `ProviderRole` (APP/SOURCE duality), `CapabilityGrant`
(ref-only — the Mission gets a `credential_ref`, never a token), and `ProductivityProvider` /
`ProductivityRegistry` projected into the reality-filter `IntegrationManifest` with partial liveness
(`live_capabilities` / `is_capability_live`). The adapter families that followed — Google Workspace
(`google_app.py`), Microsoft 365 (`microsoft_app.py`), file formats (`file_formats.py`), LibreOffice
(`libreoffice_app.py`) — and the OneDrive Source connector (`sources_onedrive.py`) are all live.
Remaining roadmap: Windows Office desktop + Apple iWork (`LOCAL_DESKTOP_AUTOMATION`) and richer
file-format engines (openpyxl/python-docx/pypdf).
