#!/usr/bin/env bash
# Local DRY RUN of everything about the Windows R0-prime pipeline that can run WITHOUT a Windows host (no network except step 5, no signing,
# nothing built, nothing installed). It does not replace a Windows run: it catches the mistakes that do not need one.
#
#   DRYRUN_PYTHON=<python with pytest + pyyaml> packaging/tools/windows-dryrun.sh
#   FROZEN_EVIDENCE_DIR=<dir with r0-audit/, native-release-evidence/> DRYRUN_PYTHON=... packaging/tools/windows-dryrun.sh   # adds the frozen Windows rows
#
# 1  static lint of every PowerShell file (kit, dated check, build driver): ASCII, LF, balanced, no system-changing command (NOT a parser)
# 2  bash -n of every packaging script (syntax only)
# 3  tests/test_windows_build_prep.py (git-bash simulation: CR in captured output, python3 shim, scrubbed env; manifest; autocrlf clone; SBOM on a
#    Windows archive; workflow Windows leg has no signing/publishing and uses locked inputs; kit/contract checks)
# 4  the pins, SBOM, notices, workflow and identity tests the Windows leg depends on
# 5  (optional, needs FROZEN_EVIDENCE_DIR, a built wheelhouse and network) the frozen v0.3.2 Windows listing through the SBOM generator + verifier
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; ROOT="$(cd "$HERE/../.." && pwd)"; cd "$ROOT"
PY="${DRYRUN_PYTHON:-python3}"
rc=0
step() { echo; echo "== $*"; }
KIT=packaging/acceptance/windows-r0prime
step "1/5 PowerShell static lint"
"$PY" -I "$KIT/tools/static_check_ps.py" packaging/build-windows.ps1 "$KIT"/*.ps1 "$KIT"/tools/*.ps1 | grep -v '^   review' || true
"$PY" -I "$KIT/tools/static_check_ps.py" packaging/build-windows.ps1 "$KIT"/*.ps1 "$KIT"/tools/*.ps1 >/dev/null || { echo "LINT FAILED"; rc=1; }
step "2/5 bash -n"
for f in packaging/*.sh packaging/tools/*.sh packaging/notices/*.sh "$KIT"/tools/*.sh; do bash -n "$f" || { echo "SYNTAX ERROR in $f"; rc=1; }; done
echo "checked $(ls packaging/*.sh packaging/tools/*.sh packaging/notices/*.sh "$KIT"/tools/*.sh | wc -l | tr -d ' ') scripts"
step "3/5 Windows build preparation tests"
env -u APPROVAL_SECRET -u SSH_AUTH_SOCK -u SSH_AGENT_PID "$PY" -m pytest tests/test_windows_build_prep.py -q || rc=1
step "4/5 dependent tests (pins, sbom, notices vs sbom, workflow, identity, launcher static)"
T=""; for t in test_packaging_pins test_packaging_sbom test_notices_vs_sbom test_workflow_no_publish test_launcher_lifecycle_static test_launcher_tauri_config test_r0prime_identity; do [ -f "tests/$t.py" ] && T="$T tests/$t.py"; done
# shellcheck disable=SC2086
env -u APPROVAL_SECRET -u SSH_AUTH_SOCK -u SSH_AGENT_PID "$PY" -m pytest $T -q || rc=1
step "5/5 frozen Windows listing through the SBOM generator + verifier (optional)"
if [ -n "${FROZEN_EVIDENCE_DIR:-}" ] && [ -d "$ROOT/packaging/wheelhouse" ]; then
  FROZEN_EVIDENCE_DIR="$FROZEN_EVIDENCE_DIR" packaging/sbom/check_frozen_gaps.sh | grep -E "windows|verifier exit" || rc=1
else
  echo "skipped (set FROZEN_EVIDENCE_DIR and build packaging/build-wheelhouse.sh first)"
fi
echo; [ "$rc" = 0 ] && echo "DRY RUN OK (this is NOT a Windows run)" || echo "DRY RUN FAILED"
exit "$rc"
