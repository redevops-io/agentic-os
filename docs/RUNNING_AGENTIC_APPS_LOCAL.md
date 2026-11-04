# Running Agentic Apps on your computer: why you see "Not signed in" (401) and how to get back in

Purpose: Agentic Apps R0-prime runs a small service on your own computer and shows its pages in your web browser. This page explains the message you may
see when a page opens but shows no data, and how to fix it in a few seconds. Technical detail is in `docs/LOCAL_API_SECURITY.md` (section 9).

## What you see

* The page opens, but there is a red bar at the top: **"Not signed in to the local Agentic Apps service"**, and no project data.
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
| Opened a **bookmark** of the app address | red bar, or the page cannot be reached (the port changes every time the launcher starts) | press **Start & open**; do not bookmark |
| Reopened a tab after restarting the browser | red bar | close it, press **Start & open** |
| **Typed or pasted** the address from the launcher window | red bar | use **Start & open** instead |
| Opened a **second tab** by hand | the second tab shows the red bar; the first still works | press **Start & open** again for each extra tab |
| Used the one-time address a second time, or after it expired | red bar | get a fresh one (**Start & open**, or restart `serve`) |
| Started the service in a **container** | the one-time address is in the container log; a different published port gives a "not allowed" answer | read the address from the log; map the same port, or ask the person who runs it to list the address you use |
| Signed in to an app with a **provider's "connect" page** that redirects back to Agentic Apps | the provider's last page shows an error instead of "Connected" | not supported in R0-prime; use a connection that takes a key or token you enter in the app |
| Opened **/docs** or **/redoc** in the address bar | an error or a blank page | these pages are not available in the browser in R0-prime |

## If it still does not work

* Make sure the launcher window says the service is running; press **Start & open** again.
* If the launcher says **"The local service is busy"**, nothing was opened; wait a moment and press **Start & open** again.
* If it says the service **"could not prove its identity"**, the launcher stopped that service and did not open the browser, on purpose. Press **Start & open** to start a fresh one.
* If the launcher warns about an "old, unauthenticated Agentic Apps service", an older version (v0.3.2) is still running on port 8787. Quit it (in Activity Monitor or Task Manager, end `redevops-sidecar`)
  and start again.
* The R0-prime launcher can be installed next to the older Agentic Apps; they are separate products, but run only one at a time.
