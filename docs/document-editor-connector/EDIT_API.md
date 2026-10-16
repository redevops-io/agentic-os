# Does stock ONLYOFFICE Document Server have a non-interactive server-side edit API?

**Answer: yes, exactly one, and it is narrower than it looks: the Document Builder service (`POST /docbuilder`).**
The Conversion service converts but never edits; CommandService acts only on live co-editing sessions; WOPI `PutFile`
is Document Server writing back to a *host* during an interactive session. Everything below was read from source and
then **executed against a real stock server**; `automated_edit` and `tracked_changes` are reported `true` only
because the integration tests named at the end pass against it.

* Server source: `github.com/ONLYOFFICE/server` tag **v9.4.0.129** (`13142e41dfc9e1c337bbe76f0222d4b2ce7efbc8`, the
  build inside the image below). Line numbers are for that tag.
* Docs: `github.com/ONLYOFFICE/api.onlyoffice.com`, `site/docs/...` (paths below).
* Real server: `onlyoffice/documentserver@sha256:3ab6ebc7c605e5a32b7ae3ff19daed4925090245acc8100ce2230bd766c88212`
  (tag `9.4.0.1`, reports `buildVersion 9.4.0 / buildNumber 129`), community edition, JWT on, throwaway container.

## Routes (DocService/sources/server.js)

| Route | Line | Purpose |
|---|---|---|
| `POST /coauthoring/CommandService.ashx`, `POST /command` | 234-236 | CommandService |
| `POST /ConvertService.ashx`, `POST /converter` | 239-240 | Conversion service |
| `POST /docbuilder` | 282 | Document Builder service |
| `GET /healthcheck` | 263 | liveness (`true`) |

## Authentication (DocService/sources/DocsCoServer.js)

`getRequestParams` (1697) merges the request body/query with the JWT payload when
`services.CoAuthoring.token.enable.request.inbox` is on (1713): body `token` (1717) else the `Authorization: Bearer`
header (`checkJwtHeader` 1681, defaults `Authorization` / `Bearer ` in Common/config/default.json:484-486).
`tokenRequiredParams: true` (default.json:349) means **only the signed payload counts**. Verified live: an unsigned
`{"c":"version"}` returns `{"error":6}`; header and body tokens both work; HS256 only.

## CommandService methods (DocsCoServer.js `commandHandle`, 4628)

| Method | Line | What it can do | What it cannot do |
|---|---|---|---|
| `info` | 4635 | users currently editing a key (only if the doc was opened in an editor; otherwise `error:1`, seen live) | no content |
| `drop` | 4650 | disconnect users from a live session | no content |
| `forcesave` | 4670 | ask a live editing session to save through its callback | needs a session; **no content change** |
| `meta` | 4687 | push document metadata to a live session | no content change |
| `getForgotten` / `deleteForgotten` / `getForgottenList` | 4695 / 4718 / 4728 | recover/delete files left by closed sessions | read/delete only |
| `version` / `license` | 4732 / 4736 | build version / licence (used for `connect_check`; seen live: `9.4.0.129`) | - |

None of them edits a document. (docs: `site/docs/docs-api/additional-api/command-service/*.md`.)

## Conversion service (converterservice.js `convertRequest`, 272)

Documented in `site/docs/docs-api/additional-api/conversion-api/{request,response,error-codes}.md`. `async:true` +
repeating the identical request polls (`endConvert`, `percent`, `fileUrl`); the result URL's host is the host the
request was sent to (seen live: `http://127.0.0.1:<port>/cache/files/data/conv_<key>_<fmt>/output.pdf/a.pdf?md5=..&expires=..`).
Converts between formats (docx->pdf, txt->docx, csv->xlsx all executed); **never edits content**.

## Document Builder service (converterservice.js `builderRequest`, 460) - the edit API

Documented in `site/docs/docs-api/additional-api/document-builder-api.md` ("Document Builder API"): request fields
`async`, `key`, `token`, `url` (absolute URL of a `.js` script), **`argument`** (JSON handed to the script),
`urls` in the response, error codes -1/-2/-3/-4/-6/-8. Source: `cmd.setBuilderParams({argument})` (485),
`cmd.setUrl(params.url)` + format `docbuilder` (491-492); a script in the POST body is also accepted (494,
undocumented; **not used** by the connector). The converter spawns the `docbuilder` binary
(FileConverter/sources/converter.js `spawnProcess` 1117; binary at 1141, `--save-use-only-names` 1143,
`--argument=` 1145, `--options=` 1147, source path 1258) and returns signed URLs for every `builderJS.SaveFile(...)`.

Scripts use `builderJS.OpenFile(<absolute url>)` ... Office API ... `builderJS.SaveFile(fmt, name)`
(`site/docs/document-builder/using-cli/script-file.md`; `builder.` vs `builderJS.` matters: with `builder.` the
globals are undefined and the job fails with error -3). Compare-to-tracked-changes is documented in
`.../using-cli/comparing-documents.md`.

### What was executed live (apps/document-editor/integration, plus the spike notes below)

| Capability | Result on the real server |
|---|---|
| open an existing docx by URL, edit, save as a new file | **works** (`OpenFile(url)` -> edit -> `SaveFile("docx", ..)`) |
| `ApiDocument.SearchAndReplace` | **works**; with `SetTrackRevisions(true)` yields a real tracked `w:del` + `w:ins` pair |
| append paragraph (`Api.CreateParagraph`, `Push`) | **works**; tracked mode yields `w:ins` |
| content controls (`Api.CreateBlockLvlSdt`) | works in the spike (a `w:sdt` appears); **not exposed** by the connector yet |
| spreadsheet cells (`Api.GetSheet(i).GetRange("B2").SetValue(..)`, formulas) | **works** (xlsx re-read: `B2=99`) |
| compare two docs -> tracked changes (`AscCommonWord.CompareDocuments`) | works (spike) |
| `argument` JSON reaches the script as global `Argument` | **works** (arrays/objects/booleans intact) - how the connector passes data |
| `url` mode, async + `key` polling, header or body JWT | **works** |
| `RemoveAllElements()` while tracking | **NOT tracked** (text vanishes with no `w:del`): the connector never uses it |
| `ApiRange.SetText` | failed with error -3 in the spike: not used |
| authorship of revisions | **no `w:author`** is written: attribution lives in the connector's receipts, not in the file |
| comments (`AddComment`) | no visible effect in the spike: not offered |

### Limits and design decisions

1. **Scripts are arbitrary JavaScript**, not a declarative patch, and the editor's script runtime is privileged. The
   connector therefore never runs caller- or document-supplied code: it serves one of two fixed, reviewable templates
   (`patch.py`) from a short-lived signed URL and passes validated operations as JSON in `argument`. A test asserts that
   the templates contain no network or dynamic-evaluation calls and that untrusted values stay data.
2. Malformed input can make the editor service fail. Only documents already in the tenant's own store are sent, the
   connector validates containers first, and failure is reported as a typed `backend_unavailable`.
3. There is no execution sandbox/limit API beyond the converter timeout; output is verified by the connector
   (below) rather than trusted.
4. WOPI (`wopiClient.js` `putFile` 817, `X-WOPI-Override: PUT` 840) makes Document Server the WOPI *client*: it
   PUTs bytes to a host the *interactive* editor session saves to. It is not an edit API and is not used.
5. Community edition limits (connections, no multitenancy) apply; nothing here needs a paid feature.

### How the connector uses it, safely

* `propose_patch` simulates the typed operation list locally on the document's own text (paragraph level), producing the
  diff preview and the **expected result**.
* `apply_patch` sends the operations to the editor once, then **verifies the editor's output against the simulation**
  (visible paragraph text, revision counts for tracked mode, cell values for xlsx) before committing anything.
* The editor's output is stored as a **new version**; the source file is never touched; a verification failure keeps the
  output as a draft (`PRESERVE_ONLY`).

## Proof tests (the capability report is only as true as these)

`OnlyOfficeEditBackend.PROOF_TESTS` names the test that proves each capability; `test_config_ledger.py` asserts the
map equals `PROVEN`, and `test_proven_capabilities.py` asserts every named test exists in the integration suite.

| Capability | Integration test (real Document Server) |
|---|---|
| `convert`, `export` | `test_convert_docx_to_pdf_verified` |
| `inspect` | `test_inspect_reports_version_and_text` |
| `open` | `test_open_session_config_is_accepted_by_server` (headless Chrome loads the editor with the signed config; `onDocumentReady` fires) |
| `automated_edit` | `test_automated_edit_docx_and_xlsx` |
| `tracked_changes` | `test_tracked_changes_produce_revisions` |

Result of the last run: **8 passed** against a fresh throwaway container (see `apps/document-editor/integration/README.md`).
Collabora and ElixirOffice have **no adapter**: every capability reports `false` and every call fails closed.
