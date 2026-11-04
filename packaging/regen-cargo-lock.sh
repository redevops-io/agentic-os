#!/usr/bin/env bash
# Regenerate deploy/launcher/src-tauri/Cargo.lock reproducibly.
#
#   1. cargo generate-lockfile            ("latest compatible today" for the CURRENT Cargo.toml)
#   2. packaging/tools/cargo_lock_pin.py  pins every crate row recovered from the frozen CI logs
#                                         (packaging/cargo-targets/ci-locked-rows-recovered.txt) with
#                                         `cargo update -p <crate> --precise <ver>`; rows the logs do not
#                                         show are moved to the newest release published before the frozen
#                                         run (crates.io API; INFERRED) when --as-of is given
#   3. packaging/tools/verify_cargo_lock.py compiles nothing: it asks `cargo metadata --locked` for the
#                                         per-target crate set and compares it with the CI compile lists
#
# When `defects` (or anyone) changes Cargo.toml (e.g. adds a Windows-only dependency), re-run this script:
# the already-pinned rows stay on the frozen versions, only the new crates resolve to "latest today",
# and the script prints which rows are new. Use  --no-asof  to skip the crates.io API step.
#
# Needs network (crates.io). Writes ONLY deploy/launcher/src-tauri/Cargo.lock (and a report to stdout).
set -euo pipefail
. "$(dirname "$0")/common.sh"
ASOF="${CARGO_LOCK_AS_OF:-2026-09-18T14:06:53Z}"   # "Locking 429 packages" in the frozen macOS leg
[ "${1:-}" = "--no-asof" ] && ASOF=""
SRC="$ROOT/deploy/launcher/src-tauri"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
command -v cargo >/dev/null || { echo "cargo not found"; exit 1; }
[ "$(rustc --version | cut -d' ' -f2)" = "$RUST_VERSION" ] || { echo "rustc is $(rustc --version), pinned $RUST_VERSION"; exit 1; }
# Work on a copy so a failed run never leaves a half-written lock in the tree.
for f in Cargo.toml build.rs src tauri.conf.json capabilities icons; do cp -R "$SRC/$f" "$WORK/"; done
BEFORE=""; [ -f "$SRC/Cargo.lock" ] && cp "$SRC/Cargo.lock" "$WORK/Cargo.lock.before"
( cd "$WORK" && cargo generate-lockfile 2>&1 | tail -3 )
ARGS=(--manifest-dir "$WORK" --rows "$PK/cargo-targets/ci-locked-rows-recovered.txt" --report "$WORK/report.json"
      --first tauri@2.11.5 tauri-build@2.6.3 serde@1.0.229 serde_json@1.0.151)
[ -n "$ASOF" ] && ARGS+=(--as-of "$ASOF")
python3 -I "$PK/tools/cargo_lock_pin.py" "${ARGS[@]}" || echo "(some recovered rows could not be matched: see above)"
python3 -I "$PK/tools/verify_cargo_lock.py" --manifest-dir "$WORK" --targets "$PK/cargo-targets" || echo "(verification reported differences)"
cp "$WORK/Cargo.lock" "$SRC/Cargo.lock"
cp "$WORK/report.json" "$PK/cargo-targets/last-regen-report.json"
echo "wrote $SRC/Cargo.lock ($(grep -c '^name = ' "$SRC/Cargo.lock") rows) and packaging/cargo-targets/last-regen-report.json"
