---
name: Bug report
about: Something does not behave as the documentation or the tests describe
labels: ["bug"]
---

<!--
If this is a security issue, do not open an issue: use the private channel in SECURITY.md.
Everything you paste here is public - redact keys, tokens, hostnames and client identifiers.
-->

**What happened**

**What should happen instead**
Point at the file and section, or at the test that encodes the expectation.

**How to reproduce**

```bash
# as exact as you can make it, from a fresh checkout if possible
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,duckdb]"
PYTHONPATH=$PWD:$PWD/apps pytest -p no:cacheprovider tests
```

**Output**

```
paste the failing output, trimmed to the relevant lines
```

**Environment**

- Commit (`git rev-parse --short HEAD`):
- OS and architecture (for example macOS 15 arm64, Ubuntu 24.04 x86_64):
- Python version (`python -V`):
- Component (`apps/...`, the kernel, a workflow):
- Skips or platform notes: on macOS some containment tests skip by name and reason because the
  platform refuses an address-space limit, so say whether you saw a skip or a failure.

**If you have a view on the fix**
