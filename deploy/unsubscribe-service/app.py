"""Monitored unsubscribe endpoint (CAN-SPAM / RFC 8058 one-click) — redevops.io/unsubscribe.

Every cold-outreach email footer links here with a per-recipient token. A GET or a one-click POST records
the opt-out to a persistent, file-backed SuppressionLedger that the outbound sender consults before every
send — so an unsubscribe is honored everywhere. The raw email never rides in the URL (the token is a
non-reversible hash); a monitored ``unsubscribe@redevops.io`` mailbox can also suppress by token/email/domain.
"""
from __future__ import annotations

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from agentic_os.world.outreach import SuppressionLedger, handle_unsubscribe

# persisted on a mounted volume so opt-outs survive restarts and are auditable
_LEDGER = SuppressionLedger(path="/data/suppression.txt")

app = FastAPI(title="ReDevOps unsubscribe")


@app.get("/healthz")
def healthz():
    return {"ok": True}


def _page(msg: str, ok: bool = True) -> str:
    color = "#15803d" if ok else "#b45309"
    return f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Unsubscribe · ReDevOps</title>
<style>body{{margin:0;background:#0f1416;color:#e6edee;font:16px/1.6 system-ui,sans-serif;
display:grid;place-items:center;min-height:100vh}}.c{{max-width:460px;padding:34px;text-align:center}}
h1{{font-size:22px;margin:0 0 10px;color:{color}}}a{{color:#39c9b8}}.m{{color:#93a3a8}}</style>
<div class="c"><h1>{'You’re unsubscribed' if ok else 'Unsubscribe'}</h1>
<p class="m">{msg}</p><p><a href="https://redevops.io">redevops.io</a></p>
<p class="m" style="font-size:12px">ReDevOps.io LLC · 20200 West Dixie Highway, Suite 902, Miami, Florida 33180</p></div>"""


@app.get("/unsubscribe", response_class=HTMLResponse)
def unsubscribe_get(u: str = "", e: str = ""):
    key = (u or e).strip()
    if not key:
        return HTMLResponse(_page("This link is missing its unsubscribe code. Email unsubscribe@redevops.io "
                                  "and we'll remove you.", ok=False), status_code=400)
    handle_unsubscribe(key, _LEDGER)
    return HTMLResponse(_page("You won't receive any further outreach from ReDevOps. This is honored within "
                              "10 business days across our systems."))


@app.post("/unsubscribe")
def unsubscribe_post(u: str = Form(default=""), e: str = Form(default="")):
    # RFC 8058 List-Unsubscribe-Post one-click
    key = (u or e).strip()
    if key:
        handle_unsubscribe(key, _LEDGER)
    return PlainTextResponse("unsubscribed")


@app.get("/api/unsubscribe/status")
def status(email: str = ""):
    return JSONResponse({"email": email, "suppressed": _LEDGER.is_suppressed(email)})
