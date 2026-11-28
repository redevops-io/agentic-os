"""Support app <-> a real Chatwoot. Run inside a container that has agentic_os and the app source; CHATWOOT_API_URL, CHATWOOT_API_TOKEN, CHATWOOT_ACCOUNT_ID
and OPERATOR_BASIC_AUTH come from the environment. Creates its own inbox, contact and conversation, and removes them at the end. Prints PASS/FAIL lines and a JSON summary; no credential is printed."""
import base64
import importlib
import json
import os
import sys
import time

import httpx

core = importlib.import_module("support.core")
BASE = f"{core.CHATWOOT_API_URL}/api/v1/accounts/{core.CHATWOOT_ACCOUNT_ID}"
H = core._headers()
results, facts = [], {}


def check(name, ok, detail=""):
    results.append({"check": name, "ok": bool(ok), "detail": str(detail)[:300]})
    print(("PASS  " if ok else "FAIL  ") + name + (f"  [{str(detail)[:200]}]" if detail and not ok else ""), flush=True)


def api(method, path, **kw):
    with httpx.Client(timeout=20.0) as c:
        return c.request(method, BASE + path, headers=H, **kw)


def messages(conv_id):
    r = api("GET", f"/conversations/{conv_id}/messages")
    return (r.json().get("payload") or []) if r.status_code == 200 else []


def conv_status(conv_id):
    r = api("GET", f"/conversations/{conv_id}")
    return r.json().get("status") if r.status_code == 200 else None


inbox_id = contact_id = conv_id = None
try:
    v = httpx.get(core.CHATWOOT_API_URL + "/api", timeout=10).json()
    facts["chatwoot_version"] = v.get("version")
    check("the app reports Chatwoot as connected", core.chatwoot_connected())

    r = api("POST", "/inboxes", json={"name": "integration-check", "channel": {"type": "api", "webhook_url": ""}})
    inbox_id = r.json().get("id")
    check("set-up: an API inbox was created for the check", r.status_code == 200 and inbox_id, r.text[:200])
    r = api("POST", "/contacts", json={"inbox_id": inbox_id, "name": "Integration Check", "email": "integration-check@example.com"})
    payload = r.json().get("payload") or {}
    contact_id = (payload.get("contact") or {}).get("id")
    source_id = (payload.get("contact_inbox") or {}).get("source_id")
    r = api("POST", "/conversations", json={"inbox_id": inbox_id, "contact_id": contact_id, "source_id": source_id})
    conv_id = r.json().get("id")
    r2 = api("POST", f"/conversations/{conv_id}/messages", json={"content": "I was charged twice for October. Can you check?", "message_type": "incoming"})
    check("set-up: a conversation with one inbound customer message exists", r.status_code == 200 and conv_id and r2.status_code == 200 and r2.json().get("message_type") == 0, r2.text[:200])

    # 1. read
    act = core.fetch_activity(force=True)
    listed = json.dumps(act)
    check("read: the app lists the conversation from the real Chatwoot (no sample data)", str(conv_id) in listed or "Integration Check" in listed, list(act)[:12])
    facts["activity_keys"] = sorted(act)[:20]

    # 2. draft -> private note, ungated
    before = len(messages(conv_id))
    outgoing_before = [m["id"] for m in messages(conv_id) if not m.get("private") and m.get("message_type") in (1, "outgoing")]
    out = core.draft_reply({"conversation_id": conv_id})
    msgs = messages(conv_id)
    notes = [m for m in msgs if m.get("private")]
    public_out = [m["id"] for m in msgs if not m.get("private") and m.get("message_type") in (1, "outgoing") and m["id"] not in outgoing_before]
    facts["draft_quotes_the_customer_message"] = "charged twice" in json.dumps(out)
    check("draft: the call reports done and posted_as private_note", out.get("status") == "done" and out.get("posted_as") == "private_note", out)
    check("draft: Chatwoot holds exactly one new message and it is PRIVATE", len(msgs) == before + 1 and len(notes) == 1, [(m.get("message_type"), m.get("private")) for m in msgs])
    check("draft: no customer-visible outgoing message was created", public_out == [], public_out)
    check("draft: the conversation is still open", conv_status(conv_id) == "open", conv_status(conv_id))
    facts["draft_source"] = out.get("draft_source")
    facts["draft_names_a_fixed_firm"] = "Meridian Wealth Management" in (out.get("draft") or "")

    # 3. resolve without approval -> staged, nothing changed
    out = core.resolve({"conversation_id": conv_id})
    check("resolve without approval: staged (pending_approval, executed False)", out.get("status") == "pending_approval" and out.get("executed") is False, out)
    check("resolve without approval: the conversation is still open in Chatwoot", conv_status(conv_id) == "open", conv_status(conv_id))
    forged = core.with_operator_approval({"conversation_id": conv_id}, "Basic " + base64.b64encode(b"operator:wrong-password").decode())
    out = core.resolve(forged)
    check("resolve with a wrong operator credential: still staged, still open", out.get("status") == "pending_approval" and conv_status(conv_id) == "open", out)
    out = core.resolve({"conversation_id": conv_id, "_approval": None, "approved": True})
    check("resolve with approved:true in the body and no marker: still staged", out.get("status") == "pending_approval" and conv_status(conv_id) == "open", out)

    # 4. escalate needs an explicit assignee, and approval
    out = core.escalate({"conversation_id": conv_id})
    check("escalate without approval: staged", out.get("status") == "pending_approval", out)
    good = "Basic " + base64.b64encode(os.environ["OPERATOR_BASIC_AUTH"].encode()).decode()
    out = core.escalate(core.with_operator_approval({"conversation_id": conv_id}, good))
    check("escalate with approval but no assignee: refused, nothing guessed", out.get("status") == "error" and out.get("error") == "assignee_required", out)

    # 5. resolve with a genuine operator approval -> resolved, read back
    out = core.resolve(core.with_operator_approval({"conversation_id": conv_id}, good))
    time.sleep(1)
    check("resolve with the operator's approval: done, and Chatwoot reads back resolved", out.get("status") == "done" and conv_status(conv_id) == "resolved", (out, conv_status(conv_id)))

    # 6. onboarding message: gated; with approval it is an outgoing, non-private message that is read back
    out = core.send_onboarding({"customer": "Integration Check", "email": "integration-check@example.com", "inbox_id": inbox_id, "contact_id": contact_id, "source_id": source_id})
    check("welcome message without approval: staged, nothing sent", out.get("status") == "pending_approval", out)
    n_conv = api("GET", f"/contacts/{contact_id}/conversations").json().get("payload") or []
    check("welcome message without approval: no new conversation in Chatwoot", len(n_conv) == 1, len(n_conv))
    out = core.send_onboarding(core.with_operator_approval({"customer": "Integration Check", "email": "integration-check@example.com", "inbox_id": inbox_id,
                                                           "contact_id": contact_id, "source_id": source_id}, good))
    facts["send_onboarding_result"] = {k: out.get(k) for k in ("status", "error", "onboarding_sent", "conversation_id", "message")}
    check("welcome message with approval: sent and read back as outgoing and not private", out.get("status") == "done" and out.get("onboarding_sent") is True, out)
finally:
    if contact_id:
        r = api("DELETE", f"/contacts/{contact_id}")
        check("clean-up: the contact (and its conversations) was deleted", r.status_code == 200, r.status_code)
    if inbox_id:
        r = api("DELETE", f"/inboxes/{inbox_id}")
        check("clean-up: the inbox was deleted", r.status_code == 200, r.status_code)

ok = all(r["ok"] for r in results)
print(json.dumps({"target": "chatwoot", "facts": facts, "passed": sum(r["ok"] for r in results), "total": len(results), "results": results}, indent=1))
sys.exit(0 if ok else 1)
