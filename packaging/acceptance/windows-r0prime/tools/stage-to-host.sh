#!/usr/bin/env bash
# Hash-check the candidate installers against an expected-hash file and print (or, with --copy, perform) the copy to an
# AUTHORIZED existing Windows test host. Default is a DRY RUN. This script creates no infrastructure and reads no keys:
# --copy simply runs scp with your own ssh configuration.
#
#   tools/stage-to-host.sh --sums SHA256SUMS --file Agentic.Apps_0.1.0_x64-setup.exe [--file other.msi]
#   tools/stage-to-host.sh --sums SHA256SUMS --file X.exe --copy tester@win11 --dest 'C:/r0p'
#
# bash or zsh (run as: bash tools/stage-to-host.sh ...).
set -u
KIT=$(cd "$(dirname "$0")/.." && pwd)
SUMS=""; FILES=(); COPY=""; DEST='C:/r0p'
while [ $# -gt 0 ]; do
  case "$1" in
    --sums) SUMS=${2:?}; shift 2;;
    --file) FILES+=("${2:?}"); shift 2;;
    --copy) COPY=${2:?USER@HOST}; shift 2;;
    --dest) DEST=${2:?}; shift 2;;
    -h|--help) sed -n 2,11p "$0"; exit 0;;
    *) echo "unknown option: $1" >&2; exit 2;;
  esac
done
[ -n "$SUMS" ] && [ -f "$SUMS" ] && [ ${#FILES[@]} -gt 0 ] || { echo "need --sums FILE (sha256sum format) and at least one --file" >&2; exit 2; }
sha() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }
ok=1
for f in "${FILES[@]}"; do
  [ -f "$f" ] || { echo "MISSING $f"; ok=0; continue; }
  b=$(basename "$f"); got=$(sha "$f")
  want=$(awk -v n="$b" '{h=$1; $1=""; sub(/^[ *]+/,""); if ($0==n) print h}' "$SUMS" | head -1)
  [ -n "$want" ] || { echo "NO ENTRY for $b in $SUMS"; ok=0; continue; }
  if [ "$got" = "$want" ]; then echo "OK    $got  $b"; else echo "FAIL  $got  $b (expected $want)"; ok=0; fi
done
[ "$ok" = 1 ] || { echo "refusing: hash check failed"; exit 1; }
echo "kit files:"; (cd "$KIT" && ls -1 *.ps1 MANUAL-CHECKLIST.md README.md)
cat <<TXT

Copy (any channel) the kit folder, the installer file(s) and the sums file to one folder on the host (for example $DEST), then on the host:
  Get-FileHash <installer> -Algorithm SHA256                      # must equal the OK lines above
  powershell -NoProfile -File .\\Run-All.ps1 -Variant exe -Exe <path-to-setup.exe> -ExpectedSha256File <path-to-sums>
  (msi: elevated PowerShell of the non-SYSTEM test user, -Variant msi -Msi <path>)
Start from the clean checkpoint (README.md, "Checkpoints").
TXT
if [ -n "$COPY" ]; then
  echo "== scp to $COPY:$DEST =="
  scp -p -- "${FILES[@]}" "$SUMS" "$COPY:$DEST/" || exit 1
  scp -pr -- "$KIT" "$COPY:$DEST/" || exit 1
fi
