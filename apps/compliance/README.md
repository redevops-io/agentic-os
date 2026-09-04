> ### Reference application for [Context Runtime](https://github.com/redevops-io/context-runtime)
>
> A focused AI system for **compliance**. Context Runtime ships a tenant that learns **which rule-family evidence to pull per finding** — in its offline benchmark the learned policy scores **3.562 vs 2.463** against a full-evidence baseline ([`examples/agentic_compliance.py`](https://github.com/redevops-io/context-runtime/blob/main/examples/agentic_compliance.py)).
>
> ```
> Context Runtime  →  ReDevOps RAG  →  Sidekick  →  Application logic
> ```
> One of the [ReDevOps](https://github.com/redevops-io) reference applications built on Context Runtime.

---

# agentic-compliance — agent layer + dashboard over a real OpenSCAP core

A sibling of [`apps/billing`](../billing/) (the reference pattern), built for the demo
tenant **Meridian Wealth Management** Instead of wrapping a long-running server, the "core" here
is the self-contained **OpenSCAP** scanner (`oscap`): it produces **REAL** XCCDF results
(rule id, title, pass/fail/notapplicable, severity) from a **CIS Ubuntu 22.04 LTS Level 1
- Server** benchmark, and the agent monitors / explains / stages remediations on them —
no cloud credentials.

```
OpenSCAP (oscap + CIS SSG datastream)        app.py (FastAPI, :8208)
   scan.sh ──scan──▶ results/scan-results.xml ──parse──▶ MD3 dashboard (Vanta/Drata style)
                     results/report.html      ──serve──▶ /report  · /api/activity · /agent/run
                                                          agentic actions: scan · explain · remediate
```

## What's real here

- **Real scanner**: `oscap` (OpenSCAP 1.2.x, `libopenscap8`) run inside a disposable
  Ubuntu container by `scan.sh`.
- **Real content**: the official ComplianceAsCode **SCAP Security Guide** datastream
  `ssg-ubuntu2204-ds.xml` (release v0.1.81), profile
  `xccdf_org.ssgproject.content_profile_cis_level1_server`.
- **Real results**: `results/scan-results.xml` is a genuine XCCDF ARF with embedded
  Benchmark (rule titles/descriptions/rationale/fix) + a TestResult. Latest scan:
  **128 pass / 6 fail / 264 not-applicable → 96% control pass rate** over 134 scored
  controls. The 6 real failing rules (all medium severity):
  - Install pam_pwquality Package
  - Enforce usage of pam_wheel for su (`use_pam_wheel_group_for_su`)
  - Ensure the Default Umask is Set Correctly in `/etc/bashrc` and `/etc/profile`
  - Ensure All World-Writable Directories Are Group-Owned (`file_permissions_ungroupowned`)
  - Verify permissions of log files (`permissions_local_var_log`)
- The dashboard parses that file directly — **no mock data**. A couple of SME compliance
  items (regulatory registration / insurance expiry) are layered in for the wealth-management context
  and appear in the failing/expiring queue (e.g. Errors & Omissions insurance expiring).

## Files

| File | Purpose |
|------|---------|
| `scan.sh` | Runs the real `oscap` scan in a container; fetches the SSG datastream if absent. Idempotent — re-scans and overwrites the results. |
| `seed.py` | Repeatable wrapper: runs `scan.sh` (skips if a cached results file exists; `--force` re-scans), writes `.env`, prints a `SEED_OK …` summary line. |
| `app.py` | FastAPI service (port 8208): `/health`, `/api/activity`, `/` dashboard, `/report`, `/agent/run`. |
| `requirements.txt` | fastapi, uvicorn, httpx. |
| `Dockerfile` | slim-python image; bakes in `results/` + `content/`, serves on 8208. |
| `content/ssg-ubuntu2204-ds.xml` | The real SCAP Security Guide datastream (CIS Ubuntu 22.04). |
| `results/scan-results.xml`, `results/report.html` | The real oscap output (cached scan + HTML report). |
| `.env` | Written by `seed.py`: `SCAP_RESULTS`, `SCAP_REPORT`, `SCAP_PROFILE`. |

## Seed (run the real scan) + run

```bash
cd apps/compliance

# 1. Produce the real OpenSCAP results (idempotent; writes .env). Uses `sudo docker`.
python3 seed.py            # skips if cached; --force to re-scan
#   → SEED_OK profile=cis_level1_server pass=128 fail=6 notapplicable=264 pass_rate=96%

# 2. Install deps + run the service
pip install -r requirements.txt          # add --break-system-packages on PEP-668 hosts
python3 -m uvicorn app:app --host 0.0.0.0 --port 8208
#   app.py auto-loads .env, so it finds the results file with no manual config.

# Or with Docker (results + content are baked into the image):
docker build -t agentic-compliance .
docker run --rm -p 8208:8208 agentic-compliance
```

`scan.sh` runs the scan directly if you want to re-scan without the wrapper:

```bash
sudo docker run --rm \
  -v "$PWD/content":/content:ro -v "$PWD/results":/results \
  ubuntu:22.04 bash -lc "apt-get update -qq && apt-get install -y libopenscap8 && \
    oscap xccdf eval --profile xccdf_org.ssgproject.content_profile_cis_level1_server \
      --results /results/scan-results.xml --report /results/report.html \
      /content/ssg-ubuntu2204-ds.xml"
```

## Environment variables

| Var | Default | Meaning |
|-----|---------|---------|
| `SCAP_RESULTS` | `results/scan-results.xml` | Path to the real oscap XCCDF results file the dashboard parses. |
| `SCAP_REPORT` | `results/report.html` | Path to the oscap HTML report served at `/report`. |
| `SCAP_PROFILE` | `…content_profile_cis_level1_server` | The XCCDF profile that was evaluated (shown in the header). |
| `PORT` | `8208` | uvicorn bind port. |
| `ANTHROPIC_API_KEY` | _(optional)_ | If set, `/agent/run` `"explain"` adds an LLM rewrite (model `claude-opus-4-8`). The endpoint works fully without it — explanations fall back to the SCAP content's own description/rationale/fix. |

## Endpoints

- `GET /health` → `{"status":"ok","core":"openscap","connected": <results file present?>}`
- `GET /api/activity` → REAL parsed findings: control pass rate %, passing/failing counts,
  top failing rules sorted by severity, a framework-status view (the CIS profile), plus the
  SME license/insurance items (with an `expiring` flag). Cached 15s; invalidated on re-scan.
- `GET /` → the MD3 compliance dashboard (Vanta/Drata style) rendered from the real scan:
  KPI tiles (control pass rate / passing / failing / open findings), a framework-status card
  with a progress bar, and a failing-&-expiring queue table. Header shows **Meridian Wealth
  Management**, a green **"agent active · core: OpenSCAP connected"** pill, and an **"Open report ↗"**
  button linking to `/report`.
- `GET /report` → the OpenSCAP-generated HTML report (the real ~2.8 MB `report.html`).
- `POST /agent/run` with `{"action": ...}`:
  - `"scan"` → re-runs the real `oscap` scan via `scan.sh` and returns the updated pass rate.
  - `"explain"` `{"rule_id": <id or short id>}` → plain-English explanation + remediation for
    a failing rule, built deterministically from the rule's own SCAP description/rationale/fix
    (LLM rewrite added if a key is set).
  - `"remediate"` `{"rule_id": ...}` → **approval-gated** (`approval_required:[policy_change]`):
    returns `{"status":"pending_approval", …}` with the exact remediation that *would* run.
    System fixes are **never auto-applied** by the agent.

## Data artifacts

`content/` and `results/` (SCAP datastreams + scan output) are **fetched/generated at deploy time**, not
vendored — they are multi-MB OpenSCAP artifacts. See the Dockerfile.
