#!/bin/bash
# The launcher crate's six ignored real-sidecar tests (launcher Rust code + the built candidate sidecar),
# sequential, under env -i with a disposable HOME/TMPDIR. Requires the staged binaries/ and licenses/ dirs (git-ignored).
# usage: run_cargo_ignored.sh <worktree-with-staged-sidecar> <target-dir> <sidecar> <stale-sidecar> <out.log>
set -u
WT="$1"; TGT="$2"; SC="$3"; STALE="$4"; OUT="$5"
: "${S:?}"; : "${GUI_WORK:?}"; : "${RDO_CHROMIUM:?}"; : "${RDO_PWCORE:?}"
cd "$WT/deploy/launcher/src-tauri" || exit 2
exec /usr/bin/env -i PATH="$S/toolchain/rust/bin:/usr/bin:/bin:/opt/homebrew/bin" HOME="$GUI_WORK/home-cargo" TMPDIR="$GUI_WORK/tmp-cargo" \
  CARGO_HOME="$S/toolchain/cargo-home" CARGO_TARGET_DIR="$TGT" LANG=en_US.UTF-8 \
  RDO_SIDECAR="$SC" RDO_STALE_SIDECAR="$STALE" PW="$RDO_PWCORE" CHROMIUM="$RDO_CHROMIUM" \
  RDO_BROWSER_CMD="/opt/homebrew/bin/node $S/r0prime/build-work/e2e_driver_wrap.mjs" \
  cargo test --locked -- --ignored --test-threads=1 > "$OUT" 2>&1
