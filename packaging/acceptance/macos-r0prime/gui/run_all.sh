#!/bin/bash
# GUI/lifecycle acceptance harness (headless-driver part). Everything runs under an env -i allowlist with a
# disposable HOME/TMPDIR; the real profile is snapshotted before and after (names + mtimes only).
#
# Prerequisites (all outside the repo): a COPY of the candidate .app (ditto from the dmg), the Rust toolchain
# (builds driver/), node + playwright-core + a chrome-headless-shell (controlled browser), python3.
#   1. edit lib/gui-env.sh (paths); `source lib/gui-env.sh`
#   2. (cd driver && cargo build --release)       # CARGO_TARGET_DIR=$GUI_WORK/target
#   3. ./run_all.sh <outdir>
# The real-launcher GUI part cannot be clicked without Accessibility: see MANUAL-REMAINDER.md.
set -u
OUT="${1:?outdir}"; mkdir -p "$OUT"
: "${GUI_DIR:?source lib/gui-env.sh first}"
export PYTHONPATH="$GUI_DIR/scenarios"
"$GUI_WORK/snap_real.sh" > "$OUT/real-profile-BEFORE.txt"
python3 "$GUI_DIR/scenarios/sc_lifecycle.py" "$OUT/lifecycle"
python3 "$GUI_DIR/scenarios/sc_negative.py"  "$OUT/negative"
python3 "$GUI_DIR/scenarios/sc_browser.py"   "$OUT/browser"
python3 "$GUI_DIR/scenarios/sc_failures.py"  "$OUT/failures"
python3 "$GUI_DIR/scenarios/sc_timing.py"    "$OUT/timing"
"$GUI_DIR/scenarios/run_cargo_ignored.sh" "$WT" "$GUI_WORK/target-launcher" "$RDO_SIDECAR" "$STALE_SIDECAR" "$OUT/cargo-ignored.log"
"$GUI_WORK/snap_real.sh" > "$OUT/real-profile-AFTER.txt"
diff "$OUT/real-profile-BEFORE.txt" "$OUT/real-profile-AFTER.txt" > "$OUT/real-profile.diff" && echo "real profile unchanged"
