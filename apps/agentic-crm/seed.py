"""Seed a couple of demo Leads/Opportunities into ERPNext CRM so the agentic-crm
dashboard renders live pipeline on a fresh instance, and write agents/agentic-crm/.env.

Idempotent: skips records that already exist. stdlib-only (no deps), same style as
the other modules' seed.py. ERPNext creds come from env (reuse the agentic-books key).
"""
import json, os, urllib.request, urllib.error

URL = os.environ.get("ERPNEXT_URL", "http://localhost:8092").rstrip("/")
KEY = os.environ.get("ERPNEXT_API_KEY", "")
SEC = os.environ.get("ERPNEXT_API_SECRET", "")
H = {"Authorization": f"token {KEY}:{SEC}", "Content-Type": "application/json"}

DEMO_LEADS = [
    {"lead_name": "Dana Whitfield", "company_name": "Northwind Logistics", "status": "Open",
     "source": "Website", "email_id": "dana@northwind.example", "industry": "Transportation"},
    {"lead_name": "Marcus Lee", "company_name": "Brightline Dental Group", "status": "Open",
     "source": "Referral", "email_id": "marcus@brightline.example", "industry": "Healthcare"},
    {"lead_name": "Priya Anand", "company_name": "Helios Solar Co-op", "status": "Replied",
     "source": "Campaign", "email_id": "priya@helios.example", "industry": "Energy"},
]

def post(doctype, doc):
    req = urllib.request.Request(f"{URL}/api/resource/{doctype}",
                                 data=json.dumps(doc).encode(), headers=H, method="POST")
    try:
        urllib.request.urlopen(req, timeout=30); return "created"
    except urllib.error.HTTPError as e:
        return f"skip ({e.code})"

def main():
    if not (KEY and SEC):
        print("set ERPNEXT_API_KEY/SECRET (reuse the agentic-books key)"); return
    for l in DEMO_LEADS:
        print(l["lead_name"], "->", post("Lead", l))
    env = os.path.join(os.path.dirname(__file__), ".env")
    with open(env, "w") as f:
        f.write(f"ERPNEXT_URL={URL}\nERPNEXT_API_KEY={KEY}\nERPNEXT_API_SECRET={SEC}\n"
                f"ERPNEXT_FRONT_URL={os.environ.get('ERPNEXT_FRONT_URL', URL)}\nPORT=8210\n")
    print("wrote", env)

if __name__ == "__main__":
    main()
