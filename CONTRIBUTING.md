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

Contributions to the applications under `apps/` are made under GNU AGPL-3.0-only (the licence of that app). Contributions to the core platform and to
everything else outside `apps/` are made under the core's licence, [LICENSE.md](LICENSE.md) (AGPL-3.0-or-later with the Commons Clause). Third-party code
keeps its original licence, copyright notice and attribution, and must be listed in the notices.

`[OWNER DECISION: contributor terms]` Whether outside contributions need a DCO sign-off, a contributor licence agreement, or neither, and for which parts
of the repository, is not decided and needs legal advice. (A DCO certifies where code comes from; it does not by itself grant relicensing rights. A CLA is
not automatically required to accept contributions.) Until it is decided, outside contributions are not being merged.
