# Manual checklist: macOS arm64 candidate, fresh standard account (GUI, Gatekeeper, process hygiene)

Nothing in the kit automates the launcher window. A person at the console of the **test account** (`README.md`, "Account prerequisites")
does each step, records `PASS`, `FAIL` or `NOT DONE` (never blank, never PASS without looking) and saves the screenshot named in the table into
`~/r0p-evidence/screenshots/` with **exactly that name**. Record the variant of the run on the first line of `~/r0p-evidence/notes.txt`:
build (unsigned dev / signed candidate), dmg sha256, quarantine variant (Q / Q-set / N, `README.md`), network state, who ran it, date.

The "Expected" column is a **prediction from the source code** (`deploy/launcher/ui/index.html`, `ui/app.js`, `src-tauri/src/main.rs`,
`agentic_os/local_capability_shim.js`, `docs/LOCAL_API_SECURITY.md`), not an observation: no one has launched this window yet. A different
result is a finding, not a failed test of the checklist. Describe what you saw in your own words in `notes.txt`.

`PRC` means: run `./post_run_checks.sh --out ~/r0p-evidence/post-<ID> [--phase running]` and attach its transcript.

## A. Preconditions

| ID | Step | Expected | Record | Screenshot |
|---|---|---|---|---|
| A-01 | System Settings > Users & Groups: select this user (i). | Standard (no "Allow this user to administer this computer"). | account type | `A-01-account-standard.png` |
| A-02 | Terminal: `xcode-select -p; ls /Applications/Xcode.app` (read-only; do **not** run `git`, `python3`, `make`). | On this Mac the Command Line Tools are installed system-wide, so a path prints; `Xcode.app` does not exist. | the output (it is a property of the machine) | `A-02-devtools.png` |
| A-03 | `./post_run_checks.sh --out ~/r0p-evidence/post-00-baseline` | all PASS; `LB-01` PASS (nothing under `~/Library`). | rows | transcript |
| A-04 | Note the network state (Wi-Fi/Ethernet on, the menu-bar icon). | online for B to G; offline only for H. | state | `A-04-network.png` |

## B. The dmg

| ID | Step | Expected | Record | Screenshot |
|---|---|---|---|---|
| B-01 | Terminal: `xattr -l <dmg>` and `shasum -a 256 <dmg>`. | variant Q/Q-set: `com.apple.quarantine: 0081;...`; the hash equals the expected file. | the attribute value, the hash | `B-01-dmg-xattr-hash.png` |
| B-02 | Double-click the dmg in Finder (do not use `open`). | A "Verifying..." progress sheet may flash; then the volume "Agentic Apps R0-prime" mounts. Signed + notarized: no warning. Unsigned: macOS may warn that the image cannot be verified; note the exact text. | dialog text, whether it needed a password | `B-02-dmg-open.png` |
| B-03 | Look at the volume window. | The app and an `Applications` link; a custom volume icon. No stray files. | what is shown | `B-03-volume-window.png` |
| B-04 | Drag "Agentic Apps R0-prime" onto the `Applications` link. | A standard user cannot write to `/Applications`: Finder asks for an **administrator name and password**. Record that prompt; authenticate only if the owner is present and wants to (then the app lands in `/Applications`). Otherwise Cancel and do B-05. | prompt text, choice | `B-04-drag-to-applications.png` |
| B-05 | If B-04 was cancelled: create `~/Applications` (Finder: Go > Home, New Folder, name `Applications`) and drag the app into it. | the app is copied. | where it lives | `B-05-copy-to-home-applications.png` |
| B-06 | Terminal: `xattr -l "<installed app>"` (path from B-04/B-05). | variant Q: the copy **carries** `com.apple.quarantine` (INFERRED: DiskImages tags the files of a quarantined image). If it does not, record that: then C below does not exercise Gatekeeper. | value | `B-06-app-xattr.png` |
| B-07 | Eject the dmg (Finder sidebar). | ejects. | | |

## C. First launch under Gatekeeper (the realistic path: Finder double-click, quarantined copy)

| ID | Step | Expected | Record | Screenshot |
|---|---|---|---|---|
| C-01 | Double-click the app in Finder. | **Signed + notarized:** "'Agentic Apps R0-prime' is an app downloaded from the Internet. Are you sure you want to open it?" naming the developer and an Open button. **Unsigned (quarantined):** a block dialog ("... cannot be opened because Apple cannot check it for malicious software" or equivalent); no Open button for a standard user. | exact dialog text, button names | `C-01-gatekeeper-first-launch.png` |
| C-02 | Signed: click Open. Unsigned: do **not** use System Settings > Privacy & Security > Open Anyway unless the owner is present (it asks for an administrator password); if done, record it. | signed: the launcher window appears (D). unsigned: stays blocked, or opens after Open Anyway. | what was needed | `C-02-open-or-blocked.png` |
| C-03 | Terminal: `xattr -l "<app>"` then `spctl -a -t exec -vv "<app>"`. | after an approved first open the quarantine value usually changes (INFERRED: `0083`-style). `spctl`: signed `accepted source=Notarized Developer ID`; unsigned `rejected`. | both outputs | `C-03-xattr-spctl-after.png` |
| C-04 | Re-open the app a second time (quit first). | no dialog the second time (signed). | | |

## D. The launcher window

| ID | Step | Expected | Record | Screenshot |
|---|---|---|---|---|
| D-01 | Look at the window. | Title "Agentic Apps R0-prime", about 560 x 640; heading "Agentic Apps R0-prime" (RC3: no "ReDevOps" heading and no logo in the page), the sub-line "Your apps as one governed system" and a second line "Private candidate, release line r0-prime."; three buttons: **Check this device**, **Start & open** (blue), **Stop**; a text box "Click \"Check this device\" to begin."; a footer button **About / Legal**; a Dock icon with the app's icon (the bundle icons are TEMPORARY neutral artwork in RC3, a rounded slate square with a pale diamond and no letters; the previous "Re" mark is not retained while D-LIC-11 is an open owner decision; what the Dock and Finder actually show is UNVERIFIED until a person looks). Not blank. | what is shown; blank window? | `D-01-launcher-window.png` |
| D-02 | Look for the "Advanced - local containers" section. | **Hidden** unless `docker` is found on the launcher's `PATH` (a Finder-launched app has the minimal `/usr/bin:/bin:/usr/sbin:/sbin`, so Docker Desktop's `/usr/local/bin/docker` is probably not found even if installed: INFERRED). The three Docker buttons (Enable local containers, Disable, Remove container data) are therefore probably **not reachable** from the GUI on a normal Mac. Record whether the section is visible. | visible yes/no | `D-02-advanced-section.png` |
| D-03 | Any dialog from macOS (Local Network, Documents/Desktop access, notifications, Accessibility)? | none expected for loopback. | every dialog, its text, your answer | `P-01-permission-dialog-<n>.png` (one per dialog) |

### RC3 additions (launcher webview CSP, About / Legal; all UNVERIFIED until a person runs them in the real window)

| ID | Step | Expected | Record | Screenshot |
|---|---|---|---|---|
| D-05 | CSP: start the app from Finder as in C-01/C-02. | The window opens and shows the page fully styled (not blank, not unstyled text). The launcher starts. (The page is served under `default-src 'none'; script-src 'self'; style-src 'self'; ...`; a mistake in the policy would show a blank or unstyled window or dead buttons.) | blank/unstyled yes/no | `D-05-csp-window.png` |
| D-06 | CSP: press every button in turn: **Check this device**, **Start & open**, **Stop** (and the three Advanced buttons if D-02 showed them). | Each one answers as in E-01, E-02, E-05, E-08: a text appears in the box; no button is dead. **Remove container data** needs TWO clicks (the first only turns it into "Armed: confirm available in a moment" and, after about 0.8 s, "Click again to confirm: remove container data"; a double-click or a second click inside that first moment does nothing; it disarms after 8 s, on Escape or when you click elsewhere); there is no native confirmation dialog. (Before RC3 this was the defect-c check; with the CSP it also proves Tauri's `ipc:` transport or its postMessage fallback works under the policy.) | per button: text shown | `D-06-buttons-under-csp.png` |
| D-07 | CSP: open the web inspector if one is available (right-click > Inspect Element needs a debug build; on a release build skip and say so) or run `log stream --predicate 'subsystem == "com.apple.WebKit"' --info` in Terminal while pressing the buttons and opening About / Legal. | No "Refused to ..." / Content-Security-Policy / blocked-content message; no `Not allowed by ACL` message for a command. A message for `plugin:window|...`, `plugin:webview|...` or `plugin:event|...` (drag, zoom, print helpers that need a permission the launcher does not grant on purpose) is expected noise: record its text. | any such message, verbatim | `D-07-console.png` |
| D-08 | About / Legal from the keyboard: Tab to **About / Legal**, press Return. | The view replaces the main view in the same window; focus is on the heading "About / Legal"; it lists Product and version (Agentic Apps R0-prime, 0.1.1, r0-prime, private-candidate, platform, commit), Copyright/warranty/licence slots, each with a visible state tag ("Pending a decision" with the text "Legal text pending decision D-LIC-..." in this build; an unverified draft would carry a red dashed border, the tag "Unverified draft, not an approved text" and its label first in the text), Source code access ("not published yet (private candidate)"), the licence files and the third-party list. The note says nothing in the view is a legal statement or clearance; nothing says a licence or legal requirement is met. | what is shown; any value that looks wrong | `D-08-about-view.png` |
| D-09 | In About / Legal: Tab to **View** next to `LICENSE`, press Return; then Escape; then Escape again. | The text of `LICENSE` appears in a scrollable box (Tab reaches it; arrow keys scroll); Escape closes it and focus returns to that View button; the second Escape returns to the main view with focus on **About / Legal**. VoiceOver (Cmd+F5) reads the heading, the placeholders and the View buttons with the file name. | what happened; VoiceOver output | `D-09-about-viewer.png` |
| D-10 | With the control plane **not** started (and again after Stop), and with Wi-Fi off: open About / Legal. | Works the same: no service and no network are needed (it reads the app's own `Contents/Resources/Licenses/about.json`). If it shows "ABOUT_RECORD_MISSING", the build record is not in the bundle: record that. | state shown | `D-10-about-offline.png` |
| D-11 | macOS menu bar: open the application menu **Agentic Apps R0-prime** > **About Agentic Apps R0-prime** (if present). | UNVERIFIED whether Tauri's default menu is shown. If an About panel opens, record its text exactly: it may print the bundle copyright line, which is outside the page and not covered by the placeholder policy (design question 8). | menu present yes/no; panel text | `D-11-menu-about.png` |

## E. The buttons (v0.3.2 defect c: all buttons were dead; defects a, b)

| ID | Step | Expected | Record | Screenshot |
|---|---|---|---|---|
| E-01 | **Check this device** (no LLM key configured). | "Working..." then text beginning `Error:` + `exit exit status: 1` (the device verdict is exit 1 when no model key is set; the UI shows non-zero exits as "Error" by design. RC3: on macOS the device is also reported BLOCKED, with the note "containment unsupported on this platform", because macOS has no containment backend, so exit 1 is expected even with a key; see docs/RUNNING_AGENTIC_APPS_LOCAL.md) followed by the report: "ReDevOps - device readiness", posture lines, `llm : ...` and a line with an arrow `-> get a free key: ...` (or the Unicode arrow). **No traceback, no `UnicodeEncodeError`, no `TypeError ... __TAURI__`.** | the whole text; whether the arrow renders | `E-01-check-device.png` |
| E-02 | **Start & open**. | "Starting the control plane..." (up to ~90 s the first time, the onefile sidecar unpacks ~80 MB), then `Running at http://127.0.0.1:<port> - opened in your browser.` The port is **not 8787** (OS-chosen). The default browser opens (F). | the text, the port, the time it took | `E-02-start-open.png` |
| E-03 | `PRC --phase running` while it runs, plus Terminal: `ps -axo pid,ppid,pgid,command \| grep -i redevops-sidecar \| grep -v grep` and `lsof -nP -iTCP -sTCP:LISTEN \| grep -i redevops`. | two sidecar processes (bootloader + Python child) in one process group, one listener on `127.0.0.1:<port>` only, one `_MEI*` directory. | rows | transcript, `E-03-running-state.png` |
| E-04 | **Start & open** a second time. | the same URL; a **second browser tab/window** opens with a fresh launch code (F-07). No second sidecar (still two processes). | text, process count | `E-04-start-twice.png` |
| E-05 | **Stop**. | "Stopped." Then `PRC` (default phase `stopped`): 0 processes, 0 listeners, 0 `_MEI*`. | text, PRC rows | `E-05-stop.png`, transcript |
| E-06 | **Stop** again. | "The control plane was not started by this app." | text | |
| E-07 | **Start & open**, then **Check this device** while it runs. | both work; Check does not stop the server. | | `E-07-check-while-running.png` |
| E-08 | Docker buttons, only if D-02 showed them: **Enable local containers** / **Disable**. | the compose command runs in `../sidekick-devops` relative to the launcher's working directory (`/` for a Finder launch): expect an `Error:` text about the missing directory or about Docker, not a crash. Do not run **Remove container data** on a machine with Docker data you care about: `confirm()` should ask first (**record whether a confirm dialog appears at all in the WebKit view**; if it does not, the purge click may do nothing or run without asking: a finding). | outputs, whether confirm appeared | `E-08-docker-buttons.png` |

## F. Browser hand-off and the capability URL

Use the default browser the system opens. Safari: Settings > Advanced > "Show full website address" so the fragment is visible.

| ID | Step | Expected | Record | Screenshot |
|---|---|---|---|---|
| F-01 | The moment the browser opens after E-02. | URL `http://127.0.0.1:<port>/#lc=<43 characters>` (macOS `open` with a fragment is UNVERIFIED: record whether the fragment survived the hand-off). | whether `#lc=` was present at any time | `F-01-browser-opens.png` (take it fast, or screen-record) |
| F-02 | After the page has loaded. | the address bar shows `http://127.0.0.1:<port>/` **without** `#lc=` (`local_capability_shim.js` calls `history.replaceState` to drop the fragment before exchanging the code). The code never goes to the server in a URL. | what the bar shows | `F-02-url-after-load.png` |
| F-03 | The page content. | the Projects UI loads **with data**: sample project "Customer Operations" and its items (the security agent saw "Customer Operations" and "Refund Sarah Chen" in a headless browser). No red banner "Not signed in to the local Agentic Apps service". | what is shown; any red banner | `F-03-ui-with-data.png` |
| F-04 | Browser history (Safari: History > Show All History, search `127.0.0.1`; Chrome: chrome://history). | the entry should **not** contain `#lc=` (replaceState updates the entry). Record what it shows: a history entry holding the code would be a (low) finding because the code is single use and expires in 60 s. | the stored URL | `F-04-history.png` |
| F-05 | Reload (Cmd-R). | the UI loads again with data (there is NO cookie: the session token lives in the tab's `sessionStorage` and the shim resumes the session with `GET /api/local/session` and the `X-Agentic-Session` header). | works / banner | `F-05-reload.png` |
| F-06 | Close the tab, open a new tab and type `http://127.0.0.1:<port>/` by hand. | the NEW tab has no token in its `sessionStorage` and no `#lc=`: expected is the UI shell with the red banner "Not signed in ..." (a new tab, a bookmark or a restored session needs a fresh launch code; press **Start & open**). Record what is shown. | works / banner | |
| F-07 | Press **Start & open** again (E-04). | a new tab opens with a **new** code and loads with data; the first tab keeps working. | both tabs | `F-07-second-open.png` |
| F-08 | Quit the **browser** completely (Cmd-Q) and reopen `http://127.0.0.1:<port>/` by hand. | `sessionStorage` is gone with the tab: the UI shell loads but the API answers 401 and the red banner "Not signed in ..." appears. Press **Start & open** to get in again. | what happens | `F-08-after-browser-quit.png` |
| F-09 | Press **Stop**, then reload an open tab. | the page fails (connection refused / blank) or shows the banner; no data. | what the tab shows | `F-09-after-stop-reload.png` |
| F-10 | `lsof -nP -iTCP -sTCP:LISTEN \| grep -i redevops` while running. | the sidecar listens on `127.0.0.1` only, never `*` or `0.0.0.0`; the listener is a process of the sidecar tree. | line | in `E-03` |

## G. Quit, crash and kill (v0.3.2 defect b: orphaned sidecar)

Start the app with **Start & open** before each row; `PRC` = `post_run_checks.sh` (default phase `stopped`). "Within 10 s" because the launcher's stdin pipe closing tells the sidecar to stop.

| ID | Step | Expected | Record | Screenshot |
|---|---|---|---|---|
| G-01 | Cmd-Q the launcher (menu: Agentic Apps R0-prime > Quit). | the sidecar stops: `PRC` shows 0 processes, 0 listeners, 0 `_MEI*`. | rows | transcript |
| G-02 | Start again; close the window with the red button (do **not** use Cmd-Q). | Tauri exits when the last window closes (the source says so, UNVERIFIED on macOS: some macOS apps stay alive without a window): record whether the Dock icon stays. Then `PRC`. If the launcher stays alive, the sidecar legitimately keeps running: `PRC --phase running`. | Dock state, rows | `G-02-window-closed.png` |
| G-03 | Start again. Find the launcher: `pgrep -x agentic-r0prime`. `kill -9 <pid>` (replace with the printed pid). Wait 5 s. `PRC`. | **0 processes, 0 listeners, 0 `_MEI*`** (the stdin tether ends the sidecar when the launcher's pipe closes). In v0.3.2 this left a server on 127.0.0.1:8787 plus an 80 MB `_MEI` directory. | rows | transcript |
| G-04 | Start again. Apple menu > Force Quit > Agentic Apps R0-prime. Wait 5 s. `PRC`. | same as G-03. | rows | `G-04-force-quit.png`, transcript |
| G-05 | Start again. Terminal: `kill -TERM <launcher pid>`. Wait 5 s. `PRC`. | the launcher dies without running its exit hook; the tether still stops the sidecar (OBSERVED only with a stand-in parent by the defects agent). | rows | transcript |
| G-06 | Start again. Log the test user out (Apple menu > Log Out) and back in. `PRC`. | nothing of the app is running after login; no login item re-started it (System Settings > General > Login Items has no entry). | rows, login items | `G-06-login-items.png` |
| G-07 | After G-03 (a hard kill) check `/Users/<user>`: `ls -la ~/.agentic-os ~/.redevops /.agentic-os 2>&1`. | not present (the sidecar's default state directory is relative to the working directory; a Finder launch starts in `/`). If a state directory appears, record its location: the product writes outside `~/Library`. | listing | `G-07-state-dirs.png` |

## H. Offline first launch (stapling: only the dmg is stapled)

Needs a **signed + notarized** candidate; for an unsigned build record `N/A`. The dmg carries the stapled ticket; the app inside does **not**
(`verify_dmg.sh` row `G-04` INFO). The result of launching an unstapled, notarized app offline is UNVERIFIED: this is that test.

| ID | Step | Expected | Record | Screenshot |
|---|---|---|---|---|
| H-01 | Fresh copy of the dmg (variant Q or Q-set), **Wi-Fi off and Ethernet unplugged** (verify: `ping -c1 -t2 1.1.1.1` fails; `route -n get default` has no interface). Mount it, drag the app to `~/Applications` (B-05), double-click it. | UNKNOWN. Possible: the same prompt as C-01 using the stapled ticket (not available for the app), an extra "cannot check online" style warning, or a block. Record exactly what appears and whether the launcher window opens. | dialog text, outcome, `spctl -a -vv "<app>"` output while offline | `H-01-offline-first-launch.png` |
| H-02 | Offline: mount the **dmg** only (double-click). | the dmg is stapled, so it should mount without an online check. | outcome | `H-02-offline-dmg-mount.png` |
| H-03 | Go online, repeat C-01 on a **new** copy (delete the earlier copy first). | the normal online first-launch prompt. | outcome | |

## I. Gatekeeper on an app copied out of the dmg (copy methods)

Mount the dmg again; each copy goes to its own folder under `~/Desktop/r0p-copies/`.

| ID | Step | Expected | Record | Screenshot |
|---|---|---|---|---|
| I-01 | Copy by Finder drag, then `xattr -l` on the copy. | variant Q: quarantine present (INFERRED). | value | `I-01-finder-copy-xattr.png` |
| I-02 | Copy by `ditto "<mounted app>" ~/Desktop/r0p-copies/ditto/"Agentic Apps R0-prime.app"`, then `xattr -l`. | same as I-01 (OBSERVED for files: `ditto` keeps the attribute). | value | `I-02-ditto-xattr.png` |
| I-03 | Launch each copy from Finder. | the same Gatekeeper behaviour as C-01 for each (signed: one prompt per copy). | outcome | `I-03-copy-launch.png` |
| I-04 | **Deliberately** strip the attribute on one copy only: `xattr -d com.apple.quarantine "<copy>"`, launch it. | no Gatekeeper dialog at all (the unsigned and the signed build both open): proves that a copy without quarantine says nothing about Gatekeeper. | outcome | `I-04-no-quarantine-launch.png` |
| I-05 | `codesign --verify --deep --strict --verbose=2 "<copy>"` on every copy. | signed: valid on disk; unsigned: fails or reports ad hoc. | output | in transcript |

## J. Uninstall

| ID | Step | Expected | Record | Screenshot |
|---|---|---|---|---|
| J-01 | Quit the app (Cmd-Q), then drag the app (and each copy from I) to the Trash, empty the Trash. A copy in `/Applications` needs the administrator password again. | the app is gone. | | `J-01-trash.png` |
| J-02 | `PRC` (phase stopped) and `./post_run_checks.sh --out ~/r0p-evidence/post-99-uninstall --compare ~/r0p-evidence/post-00-baseline/snapshot.txt`. | no process, listener or `_MEI`. `LB-01` will WARN with leftover items (typically `~/Library/WebKit/<id>`, `~/Library/Caches/<id>`, `~/Library/Preferences/<id>.plist`, `Saved Application State`): **record the list**; whether those must be removed by an uninstall step is an owner decision. No LaunchAgent (`LB-02`). | the leftover list | transcript |
| J-03 | System Settings > General > Login Items. | no entry for the app. | | `J-03-login-items.png` |
| J-04 | Delete the dmg copy from the drop folder/Downloads. | | | |

## K. Wrap-up

1. `./headless_smoke.sh "<installed app>" --out ~/r0p-evidence/smoke` (before J, while the app is still installed).
2. `./collect_evidence.sh --out ~/r0p-evidence/bundle --tester <name> --dmg <dmg> --installed-app "<app path>" --path ~/r0p-evidence/screenshots --path ~/r0p-evidence/notes.txt --path ~/r0p-evidence/post-00-baseline --path ~/r0p-evidence/post-99-uninstall --path ~/r0p-evidence/smoke`
3. Write the printed SHA-256 of `SHA256SUMS.txt` into the message that hands the bundle to the owner.
4. Switch to the owner's account and delete the test account (`README.md`).

## Screenshot list (names must match)

`A-01-account-standard.png A-02-devtools.png A-04-network.png B-01-dmg-xattr-hash.png B-02-dmg-open.png B-03-volume-window.png B-04-drag-to-applications.png
B-05-copy-to-home-applications.png B-06-app-xattr.png C-01-gatekeeper-first-launch.png C-02-open-or-blocked.png C-03-xattr-spctl-after.png D-01-launcher-window.png
D-02-advanced-section.png D-05-csp-window.png D-06-buttons-under-csp.png D-07-console.png D-08-about-view.png D-09-about-viewer.png D-10-about-offline.png D-11-menu-about.png P-01-permission-dialog-<n>.png E-01-check-device.png E-02-start-open.png E-03-running-state.png E-04-start-twice.png E-05-stop.png
E-07-check-while-running.png E-08-docker-buttons.png F-01-browser-opens.png F-02-url-after-load.png F-03-ui-with-data.png F-04-history.png F-05-reload.png
F-07-second-open.png F-08-after-browser-quit.png F-09-after-stop-reload.png G-02-window-closed.png G-04-force-quit.png G-06-login-items.png G-07-state-dirs.png
H-01-offline-first-launch.png H-02-offline-dmg-mount.png I-01-finder-copy-xattr.png I-02-ditto-xattr.png I-03-copy-launch.png I-04-no-quarantine-launch.png
J-01-trash.png J-03-login-items.png`
