# Running Agentic Apps on your computer: why you see "Not signed in" (401) and how to get back in

Purpose: Agentic Apps R0-prime runs a small service on your own computer and shows its pages in your web browser. This page explains the message you may
see when a page opens but shows no data, and how to fix it in a few seconds. Technical detail is in `docs/LOCAL_API_SECURITY.md` (section 9).

## What you see

* The page shows a short panel titled **"Sign in from the Agentic Apps R0-prime window"** that says **"Not signed in to the local Agentic Apps service"**, and no project data.
* Or a tool or a script that talks to the service gets **401** (the web word for "you are not signed in").

Nothing is broken and no data was lost. The service only talks to a browser tab that it signed in itself.

## The fix

1. Go to the **Agentic Apps R0-prime** window (the small launcher window on your desktop).
2. Press **Start & open**. A new browser tab opens and is signed in automatically.
3. Close the old tab.

If you started the service yourself from a terminal (the `serve` command) or in a container, the service printed a one-time address that starts with
`http://127.0.0.1:` and contains `#lc=`. Open that exact address in your browser within five minutes. If the five minutes are over, or you already used it, stop the service and
start it again to get a new one.

## Why it works this way

Any web page you visit could try to talk to a service running on your computer. To stop that, each browser tab has to be signed in with a link that works only once. The launcher
makes that link for you when you press **Start & open**. The sign-in lives only in that one tab; it is not saved in a cookie or a file.

## Things that do not sign you in (by design)

| What you did | What happens | What to do |
|---|---|---|
| Opened a **bookmark** of the app address | the sign-in panel, or the page cannot be reached (the port changes every time the launcher starts) | press **Start & open**; do not bookmark |
| Reopened a tab after restarting the browser | the sign-in panel | close it, press **Start & open** |
| **Typed or pasted** the address from the launcher window | the sign-in panel | use **Start & open** instead |
| Opened a **second tab** by hand | the second tab shows the sign-in panel; the first still works | press **Start & open** again for each extra tab |
| Used the one-time address a second time, or after it expired | the sign-in panel | get a fresh one (**Start & open**, or restart `serve`) |
| Started the service in a **container** | the one-time address is in the container log; a different published port gives a "not allowed" answer | read the address from the log; map the same port, or ask the person who runs it to list the address you use |
| Signed in to an app with a **provider's "connect" page** that redirects back to Agentic Apps | the provider's last page shows an error instead of "Connected" | not supported in R0-prime; use a connection that takes a key or token you enter in the app |
| Opened **/docs** or **/redoc** in the address bar | an error or a blank page | these pages are not available in the browser in R0-prime |
| Pressed **Stop** and then **Start & open** again, then used the **Back button**, a restored tab or a bookmark of the old address | the browser shows its own "cannot connect" page (the port is new every start); no Agentic Apps page explains it | open the app from the launcher again (**Start & open**) |

## Known limitations of this private build

* **Leftover `_MEI...` folders.** If the service is force-killed (for example `kill -9` on `redevops-sidecar`), one folder named `_MEI...` (about 80 MB) can stay in your temporary folder (`/var/folders/.../T`). It is safe to delete when no `redevops-sidecar` process is running. A normal **Stop**, quitting the launcher or a normal shutdown leaves nothing.
* **The Docker buttons (Enable / Disable / Remove local containers)** do not work when the app is opened from Finder or the Dock; they are for development setups started from a terminal. You can ignore that section.
* **No containment backend on macOS.** Capabilities that need the rootless containment backend (the agent gateway's `sandbox.execute`, sandboxed mission capabilities) are refused with `containment_unsupported`, and "Check this device" reports BLOCKED. See "Platform support: containment" below.
* **The app is not signed.** macOS will refuse the first launch of the unsigned `.app` (Gatekeeper), and `codesign --verify --strict` fails on it until it is signed inside-out. A copy made out of a quarantined disk image carries the quarantine flag.

## Platform support: containment

Some features run a small program (a "capability") in a contained child process: scrubbed environment, a
CPU-time limit, a process-count limit and a **memory limit**, inside a throwaway working directory. This is the
rootless containment backend (`agentic_os/mission/membrane.py`). It is used by the agent gateway's
`sandbox.execute` capability and by the mission execution plane. Support by platform:

| Platform | Containment backend | Why |
|---|---|---|
| Linux (including WSL2) | available | `setrlimit(RLIMIT_AS)` is enforced by the kernel (UNVERIFIED on this build host: not run on Linux here; covered by tests that simulate the Linux path) |
| **macOS (Apple Silicon)** | **not available** | the kernel refuses a finite memory limit: `RLIMIT_AS`, `RLIMIT_DATA` and `RLIMIT_RSS` all fail with `EINVAL` below about 466 GiB (even `ulimit -v`), so the memory ceiling cannot be enforced |
| Windows (native) | not available | the backend needs a POSIX host; run inside WSL2 |

Where there is no backend, the product **fails closed**: the capability is not started, and the caller gets
`containment_unsupported:<platform and reason>` (the agent gateway returns it as the error of `sandbox.execute`).
It is never run without its limits, and it is never run with only the limits that happen to work. The device
report ("Check this device") also says so: on macOS the device is reported BLOCKED with the reason
"containment unsupported on this platform ... not macOS", and every execution class is denied.

What this means for the macOS app in this private build: the launcher, the local service and the UI do not use the
containment backend, so starting, signing in and using the app are not affected. Not available on macOS:
running a capability through `sandbox.execute`, and the mission execution plane's sandboxed capabilities. "Check
this device" exits non-zero on macOS (a BLOCKED verdict), with or without a model key. Making containment
available on macOS needs a different mechanism (a Linux VM or container; a `sandbox-exec` profile or launchd
limits do not provide an equivalent memory cap) and an owner decision; relaxing the check is not done.

## If it still does not work

* Make sure the launcher window says the service is running; press **Start & open** again.
* If the launcher says **"The local service is busy"**, nothing was opened; wait a moment and press **Start & open** again.
* If it says the service **"could not prove its identity"**, the launcher stopped that service and did not open the browser, on purpose. Press **Start & open** to start a fresh one.
* If the launcher warns about an "old, unauthenticated Agentic Apps service", an older version (v0.3.2) is still running on port 8787. Quit it (in Activity Monitor or Task Manager, end `redevops-sidecar`)
  and start again.
* The R0-prime launcher can be installed next to the older Agentic Apps; they are separate products, but run only one at a time.
