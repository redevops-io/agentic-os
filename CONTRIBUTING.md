# Contributing

1. Fork and branch from `main`.
2. `pip install -e ".[dev,duckdb]"`, then `PYTHONPATH=$PWD:$PWD/apps pytest -p no:cacheprovider tests`
   To run the suite in an environment bound to your worktree (and have the session stop if `agentic_os` would be imported from another one), use `scripts/testenv/make_env.sh` and `run_tests.sh`; see [scripts/testenv/README.md](scripts/testenv/README.md).
   (see [docs/CONFORMANCE.md](docs/CONFORMANCE.md) for the conformance gate and per-app suites).
   On macOS ten containment tests are skipped, by name and with the reason: the platform refuses an address-space limit (`RLIMIT_AS`), so the membrane refuses to run with a memory ceiling there and
   the property cannot be tested. CI runs them on Linux, and a test fails on Linux if the runner stops allowing the limit. Exported OAuth client credentials are cleared by `tests/conftest.py`.
3. `python scripts/docs_check.py` must pass for documentation changes.
4. Documentation claims: label any number *Illustrative/synthetic*, *Internally reproduced*, *Externally reproduced*
   or *Field validated* and add a card under `docs/evidence/`. Quote licence file names and titles; do not
   characterize licences.
5. Releases: follow [docs/RELEASING.md](docs/RELEASING.md); never delete or rewrite tags.
6. Security issues: [SECURITY.md](SECURITY.md), not a public issue.

## Licence of contributions and contributor terms

* **Applications under `apps/`** (GNU AGPL-3.0-only, no Commons Clause): your contribution is made under GNU AGPL-3.0-only. Every commit must carry a
  `Signed-off-by: Your Name <you@example.org>` line (`git commit -s`), which certifies the Developer Certificate of Origin, version 1.1
  (<https://developercertificate.org/>).
* **The core platform and everything else outside `apps/`** (AGPL-3.0-or-later with the Commons Clause, [LICENSE.md](LICENSE.md)): outside contributions are
  accepted only after you have signed the ReDevOps Contributor Licence Agreement, so that RedevOps can keep the core under one licence, enforce the Commons
  Clause and relicense. `[OWNER DECISION: contributor terms]` The text of that agreement is not written yet and needs counsel; until it exists, outside
  contributions to the core are not merged.
* **Third-party code** may be contributed only if its licence is compatible with the destination above; it keeps its original licence, copyright notice and
  attribution, and must be listed in the notices.
