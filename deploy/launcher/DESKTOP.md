# Desktop shell — a delivery surface, not a control plane

The desktop app is a native window around **the same Today-first UI the browser shows**. It exists to add what a browser
can't — a tray, deep links, window restore, OS credential storage — and to hold *one* thing: which deployment to talk to.

> It must never become a second control plane. It owns **no** approvals, scheduling, case or capture storage, notification
> state, navigation rules, or agent execution. All of that is in the one authoritative backend and the one web UI, so the
> browser and the desktop can never disagree.

## Who owns what

| Concern | Owner | The shell's part |
|---|---|---|
| Today items, identity, cases, captures | **backend** (`agentic_os/today.py`, `capture.py`) | none |
| Approvals, authorization, execution, verification | **backend** (governed Mission path) | none — it cannot approve anything |
| Notification state (what's actionable, acked, quiet hours, snooze wake) | **backend** (`/today/notifications`) | reads the *content-free* `count`/`summary` for the tray tooltip; never acks, never stores |
| Navigation, deep-link routing, restore-last-place, error states | **web UI** (`src/route.ts`, `nav.ts`, `ConnectionBanner`) | maps `redevops://…` onto a URL hash; checks only that it is *shaped* sanely |
| Which deployment, and the credential for it | **shell** | URL on disk; token in the OS keychain |
| Is the runtime reachable? | **shell** (and the web UI, with the same four words) | probe + a small state machine |
| Tray, window state, single instance, deep-link registration | **shell** | native only |
| The local runtime (frozen sidecar) and optional Docker tier | existing launcher, unchanged | start/stop; it's "a deployment on this machine" |

`redevopsd`, containers, databases and runtimes stay **outside** the desktop process (packaging plan).

## What the window shows

1. **First run / not connected** → the shell's own connection screen (`ui/`): address, optional token, "Use this computer".
2. **Connected** → the *deployment's own page* (`<url>/`) — literally the browser UI, loaded from the server. No bundled
   copy of the frontend, no duplicated logic. The web UI restores the last place itself (URL hash, then `localStorage`).
3. **Runtime away** → the connection screen again, explaining it and retrying on its own; it returns to Today by itself.

The remote page is a **different origin with no IPC and no capabilities** (`capabilities/default.json` grants the local
window only). A compromised or hostile page cannot call a shell command.

## Credentials

* The token lives in the **OS credential store** — Keychain (macOS), Credential Manager (Windows), Secret Service (Linux).
  There is **no plaintext fallback**: on a machine without a store the token field is disabled and saving is refused.
* Never in the connection file (only URL + label), never in a URL (userinfo is rejected), never in a log line (`Secret` prints
  `***`; statuses carry fixed sentences only, no server text), never returned to any page (`connection_get` returns
  `has_credential: bool`).
* The bearer is attached **only to the configured origin**. Redirects are **never followed** (a redirect to a login page is
  "unauthenticated"; a token can't be forwarded to another host). Plain `http://` is refused for any non-loopback host
  unless the user allowlists it (`RDO_ALLOW_INSECURE_HOSTS`).
* **Dependency:** today the shell attaches the bearer to its own *probe* requests only. It does **not** inject it into the web
  UI's traffic — turning the shell into an auth proxy would make it part of the trust boundary. Browser-side authentication
  (tenant + actor on `/today`) is the connector workstream's job; the shell then needs nothing more than to load the page.

## Health states (same words as the web UI)

`ok` · `unavailable` (waiting may fix it) · `unauthenticated` (it refused us / redirected to a login) · `incompatible` (not ours,
or too old to have Today). Every request has a 5s timeout; the probe runs off the UI thread.

* Three consecutive `unavailable` probes → leave the page for the explanation (a blip never interrupts the user), then back
  off 2s, 4s … 30s until it answers, then return to Today. A relaunch with the runtime down shows the explanation at once.
* `unauthenticated` / `incompatible` need the user → shown at once, **not** retried forever.

## Deep links

`redevops://open?section=today&item=<id>` → `#/today/<id>` on the **configured** deployment. A link can never choose a host;
unknown parameters, odd characters and over-long values are refused (not sanitised). The shell does not know which sections
exist — the web UI allowlists them. One implementation of navigation.

## Window and tray

* Window geometry is restored (`tauri-plugin-window-state`); a second launch is routed to the running instance.
* Tray: *Open Today*, *Connection settings…*, *Quit*; the tooltip (and the macOS menu-bar count) is the server's content-free
  summary — never a title, name or message text. Closing the window hides to the tray on macOS/Windows; on Linux (many
  desktops have no tray) closing quits, so the app can never be invisible.
* Navigation is restricted to the app's own pages and the one configured origin; anything else opens in the system browser.

## Not built (deliberately, for now)

* Native OS notifications — after the in-app behaviour is stable; they will show a content-free summary only.
* Code signing / notarization, auto-update, installers, store distribution.
  **Signing is load-bearing for credentials:** the OS keychain keys access to an item on the app's code identity. An unsigned,
  rebuilt binary is a *different program* to it and is asked for permission again (observed while testing). Release builds need a
  stable signing identity, or every update will re-prompt for the stored token.
* Offline cache, multi-deployment switching, auto-start at login.

## Release order (a real dependency)

The sidecar serves the Projects UI pinned in `packaging/projects_ui.lock.json` (currently `redevops-projects` **v0.1.0**, which
predates Today). For the desktop app — and the browser — to show Today from a packaged install: merge the UI PRs → tag a new
`redevops-projects` release → bump the lock → cut an `agentic-os` release → rebuild the sidecar.

## Verified (2026-10-21) — and what was not

Real **macOS arm64 debug binary** (Rust 1.99.1, Tauri 2) against a **real deployment** (backend from the unmerged triage branch +
the web UI built from the desktop branch, because the pinned UI release predates Today). `selftest/run.py`: **18 / 18 checks passed.**

| Criterion | Result |
|---|---|
| Same Today in Chromium and the shell | the **same journey script** (capture → review → approve → delivery-confirmed → waiting) ran in both and produced **identical** results and the same five-place navigation |
| Restart restores the workspace without losing server-side state | relaunch reconnected by itself, showed the same workspace, the sent message was still *Waiting on others*, and the same server-side session id was reused |
| Unavailable runtime doesn't freeze the UI | backend killed → the window left the dead page for an explanation after **16.5 s** ("isn't responding … your work is safe"); backend restarted → back on Today **1.6 s** later; the state machine logged offline then online |
| Credentials not exposed | token absent from stdout/stderr (all runs), from the connection file, and from every file under the run directory; present in the **OS keychain**; written and read back **inside the app**; used after a restart without re-entry; removed by the app on cleanup |

**Not verified — do not read the above as covering these:**

* **Windows and Linux were not run.** They are compiled and unit-tested by `.github/workflows/desktop-shell.yml` (new); the
  keychain/Secret Service paths and the tray on those platforms have not been exercised by a human or the harness.
* A **debug, unsigned** build. Release/notarized/signed builds, installers and auto-update are untested (see signing note above).
* **Deep links end to end through the OS** (the parser is unit-tested and hostile-input-tested; OS scheme registration needs a
  bundled app), **tray rendering**, **close-to-tray**, **window-geometry restore** and **single-instance routing** compile and are
  wired but were not asserted by the harness.
* Against a **sample** deployment: the sends are simulated; no real provider, tenant auth, or user was involved.
* Found while testing, and fixed upstream: the UI blanked after the first approval against the real API (server returned a bare
  view where the UI expected a full detail) — agentic-os #503 + the UI contract fix; a mock-only test suite could not see it.

## Verifying it

```bash
cd deploy/launcher/src-tauri
cargo test --locked                                    # 31 unit + local-socket tests (policy, secrets, probe, health, deep links)
cargo build --features selftest,custom-protocol        # the test hook is compiled in ONLY with this feature
python3 ../selftest/run.py --binary target/debug/redevops-launcher --url http://127.0.0.1:8799 \
        --backend-cmd "<start the deployment>" --chromium-script <path to selftest/chromium_journey.mjs with playwright installed>
```

`selftest/` runs one journey script in **both** the real shell webview and Chromium and compares the results; relaunches the
shell to prove restore; stops/starts the deployment to prove outage handling; and checks the credential rules. It uses a per-run
keychain account and never reads the secret from outside the app (macOS asks permission when a different program does).

## Threat notes

* Hostile deployment: cannot reach IPC (remote origin), cannot make the shell navigate elsewhere (allowlisted), cannot put text
  into a status, cannot make a probe hit another path (project id charset-checked).
* Hostile deep link: shape-checked, host-less, hash-only.
* Hand-edited config: re-validated on load; a refused URL is ignored.
* Stolen config file: contains no secret.
