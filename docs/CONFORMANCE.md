# Conformance gate

The N-invariant conformance suite lives in `tests/conformance/` and runs as part of the full `tests/` run (and so in
the `pytest` CI job). This page covers what each check enforces, how to run it, how to retire allowlist entries, and
how to make the CI check mandatory.

## Run locally

```bash
export PYTHONPATH=$PWD:$PWD/apps PYTHONDONTWRITEBYTECODE=1
pip install -e ".[dev,duckdb]"            # duckdb: two durable-ledger tests need it
pytest -p no:cacheprovider tests          # full kernel suite incl. tests/conformance
pytest -p no:cacheprovider tests/conformance/test_conformance_static.py tests/conformance/test_conformance_forgery.py
for d in apps/*/; do pytest -q -p no:cacheprovider "$d"; done   # per-app suites
pytest -p no:cacheprovider apps           # also works: apps/pytest.ini sets --import-mode=importlib
git status --porcelain                    # must be empty afterwards
```

`tests` and `apps` cannot be mixed in ONE invocation (the `importlib` mode in `apps/pytest.ini` applies only when
`apps/` is the collection root); run them separately, as CI does.

## What each check enforces

| Check (file) | Enforces | Invariant |
|---|---|---|
| `test_conformance_static.py::test_n7_*` | No direct model HTTP clients in app source: string/URL literals `/chat/completions`, `api.openai.com`, `api.anthropic.com`, or references to `OpenAICompatibleModel` / `AgentConsole`. Model calls go through the governed gateway. | N7 |
| `…::test_n5_*` | No `record_outcome(...)` with a numeric literal reward; rewards must be measured. | N5 |
| `…::test_n9_*` | No hard-coded `X.XX vs Y.YY` lift/benchmark banners in app strings; benchmarks must come from data. | N9 |
| `…::test_approval_forgery_raw_request_json_*` (D1) | Every `request.json()` / `req.json()` is the direct argument of `_strip_approval(...)`. | N3/N8 |
| `…::test_approval_forgery_routes_never_read_approval_marker` (D2) | No route handler (or same-file function reachable from one) reads `"_approval"`; the marker is injected in-process only. | N3/N8 |
| `…::test_n8_gated_capabilities_*` | Every `approval_required=True` capability (built from each app's own operator builder, no network) has a handler whose source mentions `_approval` or `pending_approval`. | N8 |
| `…::test_secrets_*` (F) | No default credential literals (`umami`, `postiz`, `demo-only-change-me`) as env-var defaults / in source. | secrets |
| `test_conformance_forgery.py` | For EVERY app operator, with all handlers replaced by recorders and a socket/subprocess guard armed: `POST /invoke` with forged approval variants (`_approval:true`, `approved:true`, `_Approval`, `__approval`, nested, junk token, token signed with the wrong secret, token minted for different inputs) never delivers an approval key to a handler; a valid signed token (`mint_approval_token` + `AGENTIC_OS_APPROVAL_SECRET`) delivers the marker exactly once and a replay is rejected; with the secret unset a token is inert; real handlers of gated capabilities that stage via `pending_approval` answer `status=pending_approval, executed=False` (not a 500) to a plain or forged invoke. | N3/N8 |
| `test_conformance_catalog/fleet/suites.py` | `modules.yaml` matches the manifests; every migrated app registers conformantly; suites compose only runtime-native apps. | plan §3 |
| `test_conformance_dynamic.py` | Approved gated node executes on resume (N3); failed read-back blocks commit (N4); a real outcome changes the decision (N5). | N3, N4, N5 |
| `test_conformance_learning.py` | Only a promoted policy changes which action is favoured. | N5 |
| `test_conformance_retrieval.py` | Governed retrieval, fail-closed: tenant/app isolation, classification + egress propagation. | N6 |
| `test_conformance_privacy.py` | A private canary never leaves the boundary on any governed egress path. | N7 |
| `tests/test_hygiene_ui_untouched.py` + `tests/conftest.py` hook | The run must not modify the tracked `agentic_os/projects_ui/` bundle (session fails otherwise). CI additionally runs `git diff --exit-code` and an untracked-files check. | hygiene |

Every lint has unit tests that feed it a tiny synthetic offending source (must fail) and a clean one (must pass), so
the lint is proven to bite. The pure lint functions live in `tests/conformance/_conf_lints.py`.

N1, N2, N10 and N11 are covered by the other `tests/` modules (registry, governance, verification, sandbox); the
table lists only what `tests/conformance/` itself checks.

## Allowlists: shrinking, never growing

Known offenders are listed in `test_conformance_static.py` (`ALLOW_N7`, `ALLOW_N9`, `ALLOW_D1`, `ALLOW_F`,
`ALLOW_N8`) and `test_conformance_forgery.py` (`STAGING_NOT_EXERCISABLE`). Each entry carries a reason and an audit
id. Today: N7 13 files, N9 8 files, D1 1, D2 0, N5 0, F 4 files, N8 5 capabilities, forgery-staging 3 capabilities.

**Retire an entry:** fix the offender in the app, run the static test; the entry now emits a `StaleAllowlist`
warning (`pytest -W error::StaleAllowlist` turns it into a failure). Delete the entry in the same PR. A fix by
another PR never breaks this suite: an allowlist entry that stops matching only warns.

**Never add an entry to make a new violation pass**; the assertion message says NEW violations must be fixed.

## Make the check required (repo setting; run by the repo owner)

The workflow job is named `pytest`. Require it on `main` (this replaces any existing protection, so merge in your
current settings first; `gh api repos/redevops-io/agentic-os/branches/main/protection` shows them):

```bash
gh api -X PUT repos/redevops-io/agentic-os/branches/main/protection \
  -H "Accept: application/vnd.github+json" \
  --input - <<'JSON'
{
  "required_status_checks": {"strict": true, "contexts": ["pytest"]},
  "enforce_admins": false,
  "required_pull_request_reviews": null,
  "restrictions": null
}
JSON

# optional: also require one approving PR review
gh api -X PUT repos/redevops-io/agentic-os/branches/main/protection \
  -H "Accept: application/vnd.github+json" --input - <<'JSON'
{
  "required_status_checks": {"strict": true, "contexts": ["pytest"]},
  "enforce_admins": false,
  "required_pull_request_reviews": {"required_approving_review_count": 1},
  "restrictions": null
}
JSON
```
