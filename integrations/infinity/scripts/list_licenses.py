"""Print the licence information every installed distribution declares about itself, verbatim, as a Markdown table.

Run it in the environment you installed (``python scripts/list_licenses.py``). It quotes the ``License-Expression`` and
``License`` metadata fields, the ``License ::`` classifiers and the names of the licence files shipped in each distribution.
It does not interpret them: read the licence files themselves before relying on any row.
"""
from __future__ import annotations

import importlib.metadata as md
import re
import sys


def rows():
    for d in sorted(md.distributions(), key=lambda d: d.metadata["Name"].lower()):
        m = d.metadata
        expr = m.get("License-Expression") or ""
        lic = (m.get("License") or "").split("\n")[0][:60]
        cls = "; ".join(c.split("::")[-1].strip() for c in m.get_all("Classifier") or [] if c.startswith("License"))
        files = sorted({str(f).split("/")[-1] for f in (d.files or []) if re.fullmatch(r"(LICEN[CS]E.*|COPYING.*|NOTICE.*)", str(f).split("/")[-1], re.I)})
        yield m["Name"], d.version, expr, lic, cls, ", ".join(files)


def main() -> int:
    print("| Distribution | Version | License-Expression | License field | License classifiers | Licence files shipped |")
    print("|---|---|---|---|---|---|")
    for r in rows():
        print("| " + " | ".join((c or "-").replace("|", "/") for c in r) + " |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
