# macOS Mac App Store drafts: DRAFT, NOT USED BY ANY BUILD

Nothing in `packaging/`, `deploy/launcher/` or `.github/` references these files. They are templates for a hypothetical Mac App
Store flavour and change no build, no signature and no permission. Every key is an owner decision (see
`docs/release/MAC_APP_STORE.md`, section 5). Placeholders (`TEAMID`, the bundle identifier) are not real values.

| File | Purpose | State |
|---|---|---|
| `app.entitlements.DRAFT.plist` | Entitlements of the main executable (sandbox, network client/server, application and team identifiers) | `plutil -lint` OK; attached to an ad-hoc signed scratch bundle and read back with `codesign -d --entitlements` |
| `helper.entitlements.DRAFT.plist` | Entitlements of the nested sidecar: `app-sandbox` and `inherit` only | same |
| `Info.plist.additions.DRAFT.plist` | Info.plist keys to merge (category, minimum system version, copyright); the export-compliance key is intentionally not written | `plutil -lint` OK |

Not here, on purpose: the existing Developer ID file `packaging/macos/sidecar.entitlements.plist` (it uses
`disable-library-validation`; this directory never reuses it); any provisioning profile; any signing script; the onedir sidecar.

Check commands (local, ad-hoc identity only, scratch directory; never the production identity):

    plutil -lint app.entitlements.DRAFT.plist helper.entitlements.DRAFT.plist Info.plist.additions.DRAFT.plist
    codesign -s - -f --entitlements helper.entitlements.DRAFT.plist <scratch>.app/Contents/MacOS/<helper>
    codesign -s - -f --entitlements app.entitlements.DRAFT.plist <scratch>.app
    codesign -d --entitlements - --xml <scratch>.app | plutil -p -
