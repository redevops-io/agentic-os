#!/usr/bin/env bash
# Reproducibility check: build the SAME committed tree twice from the pins, in clean checkouts, and
# report what is and is not byte-identical (wheelhouse, sidecar archive entries/modules, launcher, SBOM).
#
#   packaging/verify-reproducible.sh [--out DIR] [--skip-launcher] [--different-paths]
#   env: REPRO_CARGO_HOME_B=<dir>   build B with a different CARGO_HOME (registry cache is re-downloaded)
#        LAUNCHER_REMAP_PATHS=0     build the launcher WITHOUT --remap-path-prefix (to show the path-embedding class)
#
# * Each build starts from `git clone` of HEAD of this checkout (committed state only), a fresh
#   wheelhouse, a fresh freeze venv, an empty CARGO_TARGET_DIR (crate download cache is shared).
# * Default: both builds use the SAME checkout path (the second build wipes and rebuilds), so a
#   difference is nondeterminism, not a path. --different-paths clones to two directories instead, to
#   expose build-path embedding.
# * The launcher is built with LAUNCHER_BUNDLES=app on macOS (no dmg: that step drives Finder).
# * Nothing is signed, published or pushed. The result is a report, not a claim: "reproducible" is only
#   said of what the comparison shows identical.
# Needs: uv, git, network (PyPI/GitHub/crates.io; first run), rustc/cargo/tauri-cli at the pinned versions on PATH.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; ROOT="$(cd "$HERE/.." && pwd)"
OUT=""; SKIP_L=0; DIFFPATH=0
while [ $# -gt 0 ]; do case "$1" in
  --out) OUT="$2"; shift 2 ;; --skip-launcher) SKIP_L=1; shift ;; --different-paths) DIFFPATH=1; shift ;;
  *) echo "unknown arg $1"; exit 2 ;; esac; done
[ -n "$OUT" ] || OUT="$(mktemp -d)/repro"
mkdir -p "$OUT"; OUT="$(cd "$OUT" && pwd)"
. "$HERE/toolchain.env"
PK="$HERE"; . "$HERE/common.sh"   # identity(): the launcher binary name comes from packaging/identity.json
export LAUNCHER_BUNDLES="${LAUNCHER_BUNDLES:-app}"
COMMIT="$(git -C "$ROOT" rev-parse HEAD)"   # fixed once: both builds use the same commit even if HEAD moves
echo "output: $OUT   commit: $COMMIT   dirty files: $(git -C "$ROOT" status --porcelain | wc -l | tr -d ' ')"
[ -z "$(git -C "$ROOT" status --porcelain --untracked-files=no)" ] || echo "WARNING: tracked files are modified; the build uses the COMMITTED state only"

build_one() { # <label> <srcdir>
  local label="$1" src="$2" t0 t1
  rm -rf "$src"; git clone -q --no-hardlinks "$ROOT" "$src"; git -C "$src" checkout -q "$COMMIT"
  echo "== build $label in $src =="
  t0=$(date +%s)
  ( cd "$src" && ./packaging/build-wheelhouse.sh && ./packaging/build-sidecar.sh && ./packaging/build-notices.sh && \
      REDEVOPS_LICENSES_DIR="$src/deploy/launcher/src-tauri/licenses" ./packaging/build-sidecar.sh && ./packaging/stage-sidecar.sh ) > "$OUT/build-$label.log" 2>&1
  t1=$(date +%s); echo "   wheelhouse+sidecar: $((t1-t0)) s"
  if [ "$SKIP_L" = 0 ]; then
    export CARGO_TARGET_DIR="$OUT/target-$label"; rm -rf "$CARGO_TARGET_DIR"
    if [ "$label" = B ] && [ -n "${REPRO_CARGO_HOME_B:-}" ]; then OLD_CH="${CARGO_HOME:-}"; export CARGO_HOME="$REPRO_CARGO_HOME_B"; mkdir -p "$CARGO_HOME"; fi
    ( cd "$src" && MANIFEST_PYTHON="$(command -v python3)" ./packaging/build-launcher.sh ) >> "$OUT/build-$label.log" 2>&1
    echo "   launcher: $(( $(date +%s) - t1 )) s"
    unset CARGO_TARGET_DIR
    if [ "$label" = B ] && [ -n "${REPRO_CARGO_HOME_B:-}" ]; then export CARGO_HOME="$OLD_CH"; fi
  fi
  ( cd "$src" && CARGO_TARGET_DIR="$OUT/target-$label" ./packaging/build-sbom.sh ) >> "$OUT/build-$label.log" 2>&1 || echo "   (SBOM verifier reported findings: see $OUT/build-$label.log)"
  echo "   total: $(( $(date +%s) - t0 )) s" | tee -a "$OUT/durations.txt"
  # collect the artifacts
  local dst="$OUT/$label"; rm -rf "$dst"; mkdir -p "$dst"
  cp "$src/packaging/dist/redevops-sidecar"* "$dst/"; cp -R "$src/packaging/wheelhouse" "$dst/wheelhouse"
  cp "$src/packaging/dist/build-manifest.json" "$dst/"; [ -d "$src/packaging/dist/sbom" ] && cp -R "$src/packaging/dist/sbom" "$dst/sbom"
  if [ "$SKIP_L" = 0 ]; then
    cp "$OUT/target-$label/release/$(identity main_binary_name)" "$dst/launcher"
    [ -d "$OUT/target-$label/release/bundle/macos" ] && cp -R "$OUT/target-$label/release/bundle/macos" "$dst/app"
  fi
}
echo "build durations (seconds):" > "$OUT/durations.txt"
SRC_A="$OUT/src-A"; SRC_B="$OUT/src-A"; [ "$DIFFPATH" = 1 ] && SRC_B="$OUT/src-B"
build_one A "$SRC_A"
build_one B "$SRC_B"

# compare (needs PyInstaller to read the archives: use the hash-locked build-tools venv)
. "$HERE/common.sh"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
uv venv --python "$PYTHON_VERSION" "$TMP/cmp" >/dev/null; CPY="$(venv_py "$TMP/cmp")"
uv pip install --python "$CPY" --require-hashes --no-deps -r "$LOCKS/build-tools-$OS.lock" >/dev/null
"$CPY" -I "$HERE/tools/compare_builds.py" "$OUT/A" "$OUT/B" --out "$OUT/reproducibility-report.md" --json "$OUT/reproducibility-report.json" \
   --path-marker "$SRC_A" --path-marker "$SRC_B" --path-marker "$OUT" --path-marker "$HOME" --path-marker "${CARGO_HOME:-$HOME/.cargo}" ${REPRO_CARGO_HOME_B:+--path-marker "$REPRO_CARGO_HOME_B"} > "$OUT/compare.log"
cat "$OUT/durations.txt"
echo "report: $OUT/reproducibility-report.md"
