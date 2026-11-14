#!/bin/bash
# usage: desktop_inspect.sh <build dir> <desktop worktree> <evidence dir>   (inspects the BUILT artifact, not the source)
set -u
B=$1; W=$2; D=$3; V=${PYTHON:-python3}
S="$B/Agentic Apps R0-prime.app/Contents/MacOS/redevops-sidecar"; mkdir -p "$D"; cd "$W"
shasum -a 256 "$S" "$B/build-manifest.json" "$B"/*.dmg > "$D/sidecar-sha256.txt"; git rev-parse HEAD > "$D/source-commit.txt"
$V -I - "$S" > "$D/archive-listing.txt" <<'PY'
import sys; sys.path.insert(0, "packaging")
from sbom.archive import ExecutableArchive
a = ExecutableArchive(sys.argv[1]); names = set(a.pyz)
mods = ["agentic_os.products","agentic_os.products.registry","agentic_os.products.client","agentic_os.products.hub","agentic_os.products.projections","agentic_os.products.tools","agentic_os.products.schemas","agentic_os.products.approvals","agentic_os.products.gates","agentic_os.products.chat","agentic_os.products.sentinel","agentic_os.products.qa","agentic_os.products.routes","agentic_os.products.principal","agentic_os.sidekick.capability"]
print("archive:", sys.argv[1]); print("PYZ modules:", len(names))
for m in mods: print(("present " if m in names else "MISSING ") + m)
sys.exit(1 if [m for m in mods if m not in names] else 0)
PY
echo "archive listing rc=$?"
env -i PATH=/usr/bin:/bin HOME=/tmp LANG=en_US.UTF-8 $V -I packaging/check-sidecar-cli.py "$S" > "$D/check-sidecar-cli.txt" 2>&1; echo "check-sidecar-cli rc=$?"
env -i PATH=/usr/bin:/bin HOME=/tmp LANG=en_US.UTF-8 $V -I packaging/check-sidecar-lifecycle.py "$S" > "$D/check-sidecar-lifecycle.txt" 2>&1; echo "check-sidecar-lifecycle rc=$?"
cp "$B/sbom/sbom-verify.txt" "$B/build-manifest.json" "$B/SHA256SUMS" "$D/"
env -i PATH="$(dirname "$V"):/usr/bin:/bin" REDEVOPS_FROZEN_SIDECAR="$S" HOME=/tmp PYTHONPATH=$PWD python -m pytest -q -p no:cacheprovider tests/test_sidecar_includes_products.py 2>&1 | tail -2 > "$D/pytest-frozen-archive.txt"; cat "$D/pytest-frozen-archive.txt"
