# What the harness could NOT do: manual remainder for the owner (macOS arm64 candidate, GUI)

Why: on the machine used for the automation, no Accessibility, Screen Recording, input-posting or
Apple-Event (Automation) permission is granted to the process tree that ran the harness
(`tools/tccprobe.c` and `tools/autoprobe.c` report this without showing a prompt), and the release launcher
has no web inspector (no `isInspectable` in the binary). So the real launcher window can be started and
its existence observed (window list), but nothing in it can be clicked, read or captured automatically.
Everything below therefore needs a person at the console (preferably the fresh standard account of
`../README.md`). This file only adds to `../MANUAL-CHECKLIST.md`; IDs refer to it.

Use the same `env`-free path as the checklist (Finder double-click), NOT the harness environment.

## Items that stay entirely manual (no automated equivalent exists)

| ID | What to look at | Expected from the headless evidence | Screenshot |
|---|---|---|---|
| D-01 | window content: title "Agentic Apps", heading "ReDevOps", three buttons, text box | OBSERVED headless only: a 560 x 640 on-screen window of owner "Agentic Apps" exists, and the page's own start-up calls arrive (see report section 1) so the page scripts and `window.__TAURI__` work. Not observed: pixels. | `D-01-launcher-window.png` |
| D-02 | "Advanced - local containers" section hidden | the page asks `docker --version` at load (OBSERVED); with no docker on the launcher's PATH it answers false, so hidden | `D-02-advanced-section.png` |
| E-01 | Check this device | `Error:` + `exit exit status: 1` + report text, no traceback (the sidecar CLI path is OBSERVED headless: `check` command, exit 1) | `E-01-check-device.png` |
| E-02 | Start & open | "Starting the control plane..." then `Running at http://127.0.0.1:<port> - opened in your browser.` (the code path is OBSERVED headless: start about 0.6 s, port ephemeral) | `E-02-start-open.png` |
| E-05/E-06 | Stop, Stop again | "Stopped." / "The control plane was not started by this app." (the strings are produced by the same code, OBSERVED headless) | `E-05-stop.png` |
| E-07 | Check while running | works, does not stop the server | `E-07-check-while-running.png` |
| E-08 | Docker buttons (only if D-02 showed them) | `failed to launch: No such file or directory (os error 2)` because `../sidekick-devops` is relative to `/` for a Finder launch (OBSERVED headless with a fake docker on PATH) | `E-08-docker-buttons.png` |
| G-01 | Cmd-Q | the launcher's exit hook stops the sidecar and deletes redirect files (hook logic OBSERVED headless as the `quit` command; the Cmd-Q event itself NOT) | transcript |
| G-02 | close the window with the red button | whether the app quits (Dock icon) | `G-02-window-close.png` |
| G-04 | Force Quit | like SIGKILL: tether stops the sidecar within about 1 s (SIGKILL of the launcher stand-in OBSERVED: 0.2 s) | transcript |
| F-04 | browser history entry | must not contain `#lc=` | `F-04-history.png` |
| F-01..F-03, F-05..F-09 with the real default browser (Safari/Firefox/Chrome) | the redirect page flow, fragment strip, banner on a new tab, reload | OBSERVED only in headless Chromium (Chromium and, in the earlier work, WebKit); real Safari and Firefox UNVERIFIED | `F-*.png` |
| B, C, H, I | dmg mount, Gatekeeper first launch, offline first launch, copy methods | see report 3.8 (quarantine observed on a dittoed copy; Gatekeeper dialog itself NOT observed: launching a quarantined unsigned app through Launch Services would run it under the owner's real HOME, so it was not done) | per checklist |

## Additions suggested by what the harness found (add to the checklist)

| New ID | Step | Why |
|---|---|---|
| E-09 | After E-02, quit the browser entirely, start it again with "restore previous session", and look at the restored tab | the harness can only emulate restored storage; a real browser may restore `sessionStorage` (session resumes while the sidecar still runs) or not (red banner). Record which, per browser. |
| E-10 | After E-05 (Stop) and a new E-02, press Back/reload in the old tab and use any bookmark made earlier | the port changes on every start: expect a browser connection error with no hint to use the launcher (OBSERVED headless: ERR_CONNECTION_REFUSED). |
| E-11 | With an old v0.3.2 app's sidecar left running (start the frozen v0.3.2 app, Start, then kill only its launcher), open the new launcher | the window text must show "WARNING: An old, unauthenticated Agentic Apps service is running on port 8787 ..." (probe OBSERVED: the new launcher's page does call 8787 at load; its text NOT seen). Quit the old sidecar afterwards (`kill <pid>`). |
| G-08 | Start, then Cmd-Q, then check `ls /tmp /var/tmp $TMPDIR | grep _MEI` | the sidecar falls back to /tmp when TMPDIR is unusable (OBSERVED); a leftover `_MEI` of about 80 MB means a non-graceful stop |
