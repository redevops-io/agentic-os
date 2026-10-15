# Contributing

1. Fork and branch from `main`.
2. `pip install -e ".[dev,duckdb]"`, then `PYTHONPATH=$PWD:$PWD/apps pytest -p no:cacheprovider tests`
   (see [docs/CONFORMANCE.md](docs/CONFORMANCE.md) for the conformance gate and per-app suites).
3. `python scripts/docs_check.py` must pass for documentation changes.
4. Documentation claims: label any number *Illustrative/synthetic*, *Internally reproduced*, *Externally reproduced*
   or *Field validated* and add a card under `docs/evidence/`. Quote licence file names and titles; do not
   characterize licences.
5. Releases: follow [docs/RELEASING.md](docs/RELEASING.md); never delete or rewrite tags.
6. Security issues: [SECURITY.md](SECURITY.md), not a public issue.

Contributions are under the repository [LICENSE.md](LICENSE.md).
