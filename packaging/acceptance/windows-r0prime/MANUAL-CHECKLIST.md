# Manual checklist (GUI, prompts, non-Latin locale)

Nothing in this kit automates the launcher window. Each item below is done by a person at the console of the test host, as the
**non-SYSTEM test user**, and recorded as `PASS`, `FAIL` or `N/A` plus the screenshot named in the table. Put the screenshots in
`<OutDir>\screenshots\` with exactly these names. If a step cannot be done, write `NOT DONE` (never leave it blank and never mark it PASS).

Take every item from the matching clean checkpoint (`README.md`, "Checkpoints"). Time-stamp the screenshots (Win+Shift+S keeps the
file time; do not edit them).

| Item | When | What to do | What to record | Screenshot |
|---|---|---|---|---|
| **M-01** SmartScreen | before step 1 of an **interactive** install | Download the candidate installer with the host's browser (so it carries the mark of the web), double-click it. | Did SmartScreen ("Windows protected your computer") appear? Publisher shown (`ReDevOps.io LLC` / `Unknown publisher`)? Which button was needed (More info, Run anyway)? Smart App Control state (Settings, Privacy and security, Windows Security, App and browser control). | `M-01-smartscreen.png`, `M-01-publisher-details.png` |
| **M-02** UAC | same | Note the UAC dialog for the msi (per-machine) and whether the exe (per-user NSIS) asked for elevation. | Verified publisher shown by UAC? | `M-02-uac-msi.png`, `M-02-uac-exe.png` |
| **M-03** MOTW | after M-01 | In PowerShell: `Get-Content <file> -Stream Zone.Identifier` for the downloaded file and for the installed `agentic-r0prime.exe` / `redevops-sidecar.exe`. | ZoneId value; whether the installed binaries carry a mark. | `M-03-zone-identifier.png` |
| **G-01** first start | after install | Start Menu, "Agentic Apps R0-prime" (window title "Agentic Apps R0-prime"). | Window appears? Title, size, content rendered (not blank)? A console window flashing? | `G-01-first-start.png` |
| **G-02** Check this device | | Click "Check this device" with **no** LLM key set. | Text shown. Is there an "Error" banner, a traceback, the line with the arrow (`get a free key`) or its ASCII form? (defect a) | `G-02-check-device.png` |
| **G-03** Start & open | | Click "Start & open". | Browser opens `http://127.0.0.1:8787`? Page loads? Console window visible? | `G-03-start-open.png`, `G-03-browser.png` |
| **G-04** all buttons | | Click Stop, Docker buttons (Docker not installed: expect a clear message, not a crash). | Each button: works / error text. The buttons were dead in v0.3.2 (defect c). | `G-04-buttons.png` |
| **G-05** Stop | after G-03 | Click Stop, then in PowerShell: `Get-Process redevops-sidecar; Get-NetTCPConnection -LocalPort 8787 -State Listen`. | Any process left, port still listening (defect b)? | `G-05-after-stop.png` |
| **G-06** quit | after G-03 | Start again, close the window with the X button. Same two commands. | Sidecar left running after quit? | `G-06-after-quit.png` |
| **G-07** second start | after G-06 | Start the launcher twice in a row (double-click twice). | Two windows? Two sidecars? Error shown? | `G-07-double-start.png` |
| **G-08** crash | any time | Note every error dialog / Windows Error Reporting / Event Viewer entry (Application log, source `Application Error`). | Text and time. | `G-08-errors.png` |
| **P-01** port conflict in the window | with `09-Launcher-Lifecycle.ps1 -Test PortConflict` (or by hand: a stale old service listening on 127.0.0.1:8787) | Click "Start & open". | Does it start and open? Is the legacy-service warning shown (copy the text)? Any "port in use" error (there must not be one: the port is chosen by the OS)? | `P-01-port-conflict.png` |
| **P-02** busy / untrusted messages | with `09 -Test StartBusy` / `StartUntrusted` | Copy the exact message the window shows. | "busy" vs "could not prove its identity ... stopped"; no secret, no code, no port-secret in the text. | `P-02-busy.png`, `P-02-untrusted.png` |
| **P-03** restored tab, bookmark, direct URL | after a successful "Start & open" | In the browser: close the tab and press Ctrl+Shift+T (reopen); bookmark the address and open the bookmark in a new tab; paste `http://127.0.0.1:<port>/` by hand. | The page may load but shows no data / a banner (the API answers 401): intentional. Then click Open in the launcher again: does a fresh tab work? Does the old tab keep working until reload? | `P-03-restored-tab.png`, `P-03-direct-url.png` |
| **P-04** console flash | every first click of "Start & open" and "Check this device" | Watch the screen (film it with Win+Alt+R / phone if needed). | Any black console window flashing? (the sidecar is started with CREATE_NO_WINDOW; the automatic poll in step 9 can miss a window shorter than 150 ms) | `P-04-console-flash.mp4` or `.png` |
| **P-05** Task Manager kill | after "Start & open" | Task Manager, Details: end the launcher process with "End task" (not the sidecar). | Within 10 s: no `redevops-sidecar` process left, no listener (the Job Object path; step 9 `KillOnClose` automates the same with Stop-Process -Force). | `P-05-task-manager.png` |
| **B-01** non-Latin ANSI | separate snapshot | On a **disposable** snapshot only: Settings, Time and language, Language and region, Administrative language settings, Change system locale (e.g. Russian or Japanese), reboot. Then run `04-Regression-Bootstrap.ps1`. | `ACP` printed by the script, PASS/FAIL of rows BST-01..04. The locale change is a system setting: never do it on a machine that is not a throw-away snapshot. | `B-01-locale.png`, the transcript |
| **F-01** firewall | first run of "Start & open" | Watch for a Windows Defender Firewall dialog. | Dialog seen yes/no. If yes: which program, which networks were ticked by default. (Loopback-only listeners are not expected to prompt.) Do **not** click Allow on a public network. | `F-01-firewall.png` |
| **N-01** non-admin | all install steps | Confirm the session user is a **standard user** (Settings, Accounts, Your info: "Standard"; or `whoami /groups` has no `S-1-5-32-544` enabled). | The msi needs an elevated session: what error did a standard user get (script row INST-06)? Did the NSIS exe install without elevation? | `N-01-account.png` |
| **U-01** uninstall visuals | after step 7 | Settings, Apps, Installed apps. | Is "Agentic Apps R0-prime" gone (and, if the frozen product was installed, is "Agentic Apps" still there)? Leftover Start Menu tile? | `U-01-settings.png` |
| **R-01** reboot | optional | Restart; look for the sidecar, the Run keys (`reg query HKCU\Software\Microsoft\Windows\CurrentVersion\Run`), scheduled tasks created by the app. | Anything started or registered at boot. | `R-01-after-reboot.png` |

Cleanup after the manual part: close the window, run `07-Uninstall.ps1` (or roll the checkpoint back).
