<!--
Everything below is public: redact keys, tokens, hostnames and client identifiers from descriptions,
logs and diffs. Security reports go to the private channel in SECURITY.md, not through a pull request.
-->

## What changes, and why

One or two paragraphs. Link the issue, or name the audit item or documented limitation being addressed.

## Evidence

State what was actually run, with counts, and mark claims honestly: *Illustrative/synthetic*,
*Internally reproduced*, *Externally reproduced* or *Field validated*. Numbers that were not
reproduced outside this machine say so.

- Tests run (exact command and result):
- Platform(s) tested (the macOS skips in the containment suite are not equivalent to a Linux run):
- CI: link the run, and say which checks had not finished when you opened this

```
paste the summary line, e.g. "512 passed, 3 warnings"
```

## Checklist

- [ ] `pip install -e ".[dev,duckdb]"` then `PYTHONPATH=$PWD:$PWD/apps pytest -p no:cacheprovider tests` is green, or the failures are named and explained above
- [ ] The app's own suite was run when `apps/...` changed
- [ ] `python scripts/docs_check.py` passes, and documentation claims match the code rather than intent
- [ ] New environment variables are documented only where they are read, and added to the app's configuration contract if it has one
- [ ] A test exists that fails if this change is reverted
- [ ] No security control was weakened to get a check green (no skipped tests, no relaxed assertions, no `continue-on-error`)
- [ ] No credential, private path, internal hostname or client identifier in the diff
- [ ] Release surface untouched: no tags, no pushes to release branches, no publication (see `docs/RELEASING.md`; tags are never deleted or rewritten)
- [ ] Anything needing a licensing, attribution or publication decision is called out below rather than assumed

## Needs a decision from a maintainer

Delete this section if empty. Anything touching licence files, third-party attribution, contributor
terms, publication or release identity belongs here rather than in the diff.
