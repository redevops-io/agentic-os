# Integration checks against real cores

Three scripts that run an app's own code against a **real** instance of the product it works with. The app tests in this repository use
stand-ins for those products; these scripts are the evidence that the integration works against the real thing.

`run_with_throwaway_core.sh chatwoot|lago|twenty` starts a throwaway instance in Docker, runs the matching script and removes everything
again. The `integration-checks` workflow runs it for all three when one of the apps or the checks change, and it is run on `main` before a
release ([docs/RELEASING.md](../../docs/RELEASING.md)).

| Script | App | Product | Last run |
|---|---|---|---|
| `check_support_chatwoot.py` | `apps/support` | Chatwoot | 2026-11-24, Chatwoot 4.18.0 (Community Edition): 20 of 20 |
| `check_billing_lago.py` | `apps/billing` | Lago | 2026-11-24, Lago v1.53.0 (open source): 18 of 18 |
| `check_outreach_twenty.py` | `apps/outreach-engine` | Twenty | 2026-11-24, Twenty v2.27.0: 12 of 12 |

One run of one version shows what happened then. It is not a compatibility statement for other versions.

## What they establish

* **Support:** a draft arrives as one private note and no customer-visible message; resolve, escalate and the welcome message are only
  staged without an approval (also with a wrong operator password, and with `approved: true` typed into the request); with an approval the
  write happens and Chatwoot reads it back.
* **Billing:** a retry needs an account and an approval; only the named account's overdue invoice is sent to Lago; an invoice that did not
  settle is reported as not settled. On open-source Lago an approved refund is reported as **not executed**, because Lago answers
  `403 feature_unavailable` to a credit note.
* **Outreach:** nothing is created in Twenty without an approval; with one, Twenty reads back one company, one contact and one
  opportunity linked to both; a second approval creates no duplicate.

## Running one

Use a throwaway instance. **Never the instance that holds real data**: the scripts create and delete records.

```sh
# from the repository root, with the app's requirements installed
export PYTHONPATH=.:apps AGENTIC_OS_PROFILE=development

# Support: creates its own inbox, contact and conversations, and deletes them at the end
CHATWOOT_API_URL=http://127.0.0.1:3003 CHATWOOT_ACCOUNT_ID=1 CHATWOOT_API_TOKEN=... OPERATOR_BASIC_AUTH=operator:... \
  python scripts/integration-checks/check_support_chatwoot.py

# Billing: `setup` creates two customers with one invoice each and prints their ids; mark those invoices overdue in Lago
# (time cannot be advanced through the API), then `run`
LAGO_API_URL=http://127.0.0.1:3000 LAGO_API_KEY=... python scripts/integration-checks/check_billing_lago.py setup > ids.json
LAGO_API_URL=http://127.0.0.1:3000 LAGO_API_KEY=... python scripts/integration-checks/check_billing_lago.py run "$(cat ids.json)"

# Outreach: the two GitHub look-ups the app makes for a lead are replaced by fixed stand-ins; Twenty itself is real
TWENTY_URL=http://127.0.0.1:3000 TWENTY_API_KEY=... python scripts/integration-checks/check_outreach_twenty.py
```

Each prints `PASS` / `FAIL` lines and a JSON summary, and exits non-zero if any check failed. No credential is printed.

## Limits

* The workflow is not a required check: it pulls large third-party images. It runs one version of each product, the one named in
  `run_with_throwaway_core.sh`.
* The throwaway Twenty is seeded with Twenty's own development workspace, because a fresh instance has no workspace to create a key in.
* The Billing script's refund checks branch on what the Lago edition does with a credit note; the licensed branch has not been run.
* Update paths in Twenty (changing an existing record) are not exercised.
