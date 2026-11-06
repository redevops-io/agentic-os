# Windows MSIX draft: DRAFT, NOT USED BY ANY BUILD

Nothing in `packaging/`, `deploy/launcher/` or `.github/` references these files. They change no build, signature or permission.
See `docs/release/MICROSOFT_STORE.md` for the options, the policy findings and the owner decisions, and
`docs/release/WINDOWS_BUILD_BLOCKERS.md` for what cannot be built or tested on the macOS host.

| File | Purpose | State |
|---|---|---|
| `AppxManifest.DRAFT.xml` | Draft manifest for a full-trust (`runFullTrust`) x64 package of the launcher with the sidecar beside it | Well-formed (`xmllint --noout`). `makeappx.exe` is not available on the macOS host that wrote it, so the package was NOT built or validated against the manifest schema, not signed, and not run through the Windows App Certification Kit |

Placeholders to replace, all owner or Partner Center values: `Identity/@Name`, `Identity/@Publisher` (Store-issued CN; for a
Store submission Microsoft re-signs, so no own certificate is needed, but a sideload test needs a certificate whose subject
equals Publisher), `PublisherDisplayName`.

Not created: `Assets\StoreLogo.png`, `Square150x150Logo.png`, `Square44x44Logo.png` (the manifest references them), a
packaging script, an `.msixupload`, WebView2 handling.

Sketch of the packaging step (NOT run; Windows SDK required):

    makeappx pack /v /d <staged layout dir> /p AgenticApps.msix
    (layout: AppxManifest.xml, Assets\, agentic-r0prime.exe, redevops-sidecar.exe, Licenses\)

First test on Windows, before anything else: start the launcher, confirm the sidecar handshake succeeds and the installed
browser reaches `http://127.0.0.1:<port>/` (loopback behaviour of a full-trust package is unverified).
