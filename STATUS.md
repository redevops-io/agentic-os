# Status

Last verification date: **2026-10-15**, commit `2325dc8` (`Release 0.3.34`), package `0.3.34`, Linux x86_64, Python 3.12.

**Maturity: alpha, reference implementation; deployed internally (self-reported, no public artifact).** Not
independently validated. "Production-ready" is not claimed.

## Shipped and tested

| Item | Evidence |
|---|---|
| Kernel test suite incl. N-invariant conformance gate | `pytest tests` -> **3229 passed, 39 skipped** (2026-10-15, `pip install -e ".[dev,duckdb]"`, without the connector fixtures env var, which may account for some skips). CI: `.github/workflows/tests.yml` |
| Per-app suites | CI `tests` workflow (not re-run for this change) |
| `agentic-os modules` lists the catalog | run 2026-10-15: 21 modules, matches `modules.yaml` |
| Mission kernel: governed plan, approval gates, event-sourced replay | `tests/`, [docs/CONFORMANCE.md](docs/CONFORMANCE.md), [docs/COMPENSATION.md](docs/COMPENSATION.md), [docs/MISSION_FORK_JOIN.md](docs/MISSION_FORK_JOIN.md) |
| Control-plane auth (mutating endpoints fail closed) | `tests/test_kernel_residuals.py`, [docs/CONTROL_PLANE_SECURITY.md](docs/CONTROL_PLANE_SECURITY.md) |
| Installing as a package from a git tag | the `mission-sdk` clean-room run installed `agentic-os` `v0.3.31` this way on 2026-10-15 (see mission-sdk `docs/QUICKSTART_VERIFIED.md`) |

## Proposed / not verified

- `docker compose up -d` and the `agentic-os up ...` fleet path: **not run** for this change.
- The hosted demo at demo.redevops.io: reachability only (HTTP 200 in the inventory); behaviour not verified.
- Go port in [`go/mission/`](go/mission/): not run.
- Wiring of classification-based model routing ([`governance/routing.py`](agentic_os/governance/routing.py)) into the default router: not audited.
- Any figure for the share of work kept on local hardware (README design thesis only).

## Limitations

- Alpha API; many modules in `agentic_os/` and `apps/` are reference implementations with seed data.
- Router example config is illustrative.
- Licence: `LICENSE.md` is titled "source-available, not open source" (AGPL-3.0-or-later WITH Commons-Clause)
  while GitHub's label reads "AGPL-3.0" and `apps/*/LICENSE` are plain AGPL text. Counsel item; nothing decided here.

## Known gaps

- GitHub releases stop at `v0.3.30` while tags run to `v0.3.34` (see [docs/RELEASING.md](docs/RELEASING.md)).
- Counts differ by source: `modules.yaml` has 21 modules; `apps/` has 21 subdirectories (including helpers such as
  `_suite-server`, `infra`, `sky`); other READMEs say 15 or six. See
  [docs/evidence/aos_inventory_counts.yaml](docs/evidence/aos_inventory_counts.yaml).
- No ReDevOps package is published on PyPI. PyPI `sidekick` and `agent-harness` are unrelated third-party projects.

## Evidence

[docs/evidence/](docs/evidence/) · [docs/CONFORMANCE.md](docs/CONFORMANCE.md) · CI: `.github/workflows/tests.yml`
