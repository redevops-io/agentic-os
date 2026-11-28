"""Outreach app <-> a real Twenty (a throwaway instance seeded with Twenty's own development workspace; the operational CRM is not touched). TWENTY_URL and TWENTY_API_KEY come from the
environment. The lead is a made-up account; the two GitHub look-ups the app makes for a lead are replaced by fixed stand-ins so that nothing outside Twenty is contacted. Twenty itself is real."""
import importlib
import os
import json
import sys
import urllib.error
import urllib.request

core = importlib.import_module("outreach-engine.core")
U, K = core.TWENTY_URL, core.TWENTY_API_KEY
results, facts = [], {}


def check(name, ok, detail=""):
    results.append({"check": name, "ok": bool(ok), "detail": str(detail)[:400]})
    print(("PASS  " if ok else "FAIL  ") + name + (f"  [{str(detail)[:260]}]" if detail and not ok else ""), flush=True)


def rest(path):
    req = urllib.request.Request(U + path, headers={"Authorization": f"Bearer {K}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def named(obj, field, value):
    rows = rest(f"/rest/{obj}?limit=60&order_by=createdAt[DescNullsLast]").get("data", {}).get(obj, [])
    return [x for x in rows if (x.get(field) if not isinstance(x.get(field), dict) else json.dumps(x.get(field))) and value in json.dumps(x.get(field))]


ACCT = "northwind-check"
core._gh_profile = lambda login: {"name": "Northwind Check Ltd", "blog": "https://northwind-check.example.com", "html_url": "https://github.com/" + login, "login": login}
core._gh_top_contributor = lambda full: {"name": "Ada Lovelace", "login": "ada-check", "html_url": "https://github.com/ada-check", "email": "ada@northwind-check.example.com"}
row = {"account": ACCT, "source": "github", "full_name": f"{ACCT}/engine", "stars": 400, "url": "https://github.com/northwind-check/engine"}
core._STATE["accounts"] = [row]
core._STATE["approved"] = set()

check("the app reports Twenty as connected", core.twenty_connected())
before = {o: len(named(o, "name", "Northwind Check")) for o in ("companies", "opportunities")}
before["people"] = len(named("people", "name", "Lovelace"))
check("before: Twenty holds no record of this lead", before == {"companies": 0, "opportunities": 0, "people": 0}, before)

out = core.approve({"account": ACCT})
check("approve without an approval: staged (pending_approval, executed False)", out.get("status") == "pending_approval" and out.get("executed") is False and out.get("lead_synced") is False, out)
out = core.approve({"account": ACCT, "approved": True, "_approval": None})
check("approve with approved:true typed into the request and no marker: still staged", out.get("status") == "pending_approval", out)
mid = {o: len(named(o, "name", "Northwind Check")) for o in ("companies", "opportunities")}
check("after two unapproved requests Twenty still holds nothing for this lead, and the app has not marked it approved", mid == {"companies": 0, "opportunities": 0} and ACCT not in core._STATE["approved"], mid)

out = core.approve({"account": ACCT, "_approval": {"approved": True, "by": "integration-check"}})
check("approve with an approval: the app reports the pilot as synced", out.get("ok") is True and out.get("twenty_synced") is True, out)
companies, people, opps = named("companies", "name", "Northwind Check"), named("people", "name", "Lovelace"), named("opportunities", "name", "Northwind Check")
check("Twenty reads back exactly one company, one person and one opportunity", (len(companies), len(people), len(opps)) == (1, 1, 1), (len(companies), len(people), len(opps)))
if companies and people and opps:
    c, p, o = companies[0], people[0], opps[0]
    facts["created"] = {"company": c.get("name"), "domain": (c.get("domainName") or {}).get("primaryLinkUrl"), "person": p.get("name"), "job": p.get("jobTitle"),
                        "opportunity": o.get("name"), "amount": o.get("amount"), "stage": o.get("stage"), "closeDate": o.get("closeDate")}
    check("the person is linked to the company and carries the contact's address", p.get("companyId") == c.get("id") and (p.get("emails") or {}).get("primaryEmail") == "ada@northwind-check.example.com", p)
    want = core._pilot_amount(row) * 1_000_000
    check("the opportunity is linked to both, in stage NEW, with the amount the app computed, in USD", o.get("companyId") == c.get("id") and o.get("pointOfContactId") == p.get("id") and o.get("stage") == "NEW"
          and (o.get("amount") or {}).get("amountMicros") == want and (o.get("amount") or {}).get("currencyCode") == "USD", o.get("amount"))
listed = core.twenty_opportunities(20)
check("the app's own read of Twenty lists the new opportunity with a link to it", any("Northwind Check" in json.dumps(x) and "/object/opportunity/" in json.dumps(x) for x in listed), listed[:2])

# a second approval of the same lead: what Twenty holds afterwards is recorded as a fact, and checked against what the app says
out2 = core.approve({"account": ACCT, "_approval": {"approved": True}})
n_companies, n_opps = len(named("companies", "name", "Northwind Check")), len(named("opportunities", "name", "Northwind Check"))
facts["after_second_approval"] = {"reported_synced": out2.get("twenty_synced"), "companies": n_companies, "opportunities": n_opps}
check("a second approval of the same lead does not create a second company or opportunity", (n_companies, n_opps) == (1, 1), facts["after_second_approval"])

# a wrong key: nothing is written and the app does not claim a sync
core.TWENTY_API_KEY = "not-a-key"
out3 = core.approve({"account": ACCT, "_approval": {"approved": True}})
core.TWENTY_API_KEY = K
check("with a key Twenty refuses, the app reports the lead as NOT synced", out3.get("twenty_synced") is False, out3)
facts["twenty_version"] = os.environ.get("CORE_VERSION", "not stated")
ok = all(r["ok"] for r in results)
print(json.dumps({"target": "twenty", "facts": facts, "passed": sum(r["ok"] for r in results), "total": len(results), "results": results}, indent=1, default=str))
sys.exit(0 if ok else 1)
