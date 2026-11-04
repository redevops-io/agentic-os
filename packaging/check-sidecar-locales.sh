#!/usr/bin/env bash
# Acceptance check for the FROZEN sidecar (R0-prime defect a): `bootstrap` with stdout redirected to a
# file must print the same bytes and return the same verdict in every locale / Python encoding env.
# The frozen binary ignores PYTHONIOENCODING and PYTHONUTF8 (isolated interpreter), so the only thing
# that can differ between runs is the locale codec, and the entry script must neutralise that.
#
#   packaging/check-sidecar-locales.sh [path/to/redevops-sidecar]    (default packaging/dist/redevops-sidecar)
#
# Exit 0 only when every variant is byte-identical to the UTF-8 baseline (including the exit code).
# Locales that are not installed on this host are skipped and reported. On Windows the same check is
# `redevops-sidecar.exe bootstrap > out.txt` under a cp1252 system code page (see the R0-prime report).
set -uo pipefail
BIN="${1:-$(cd "$(dirname "$0")" && pwd)/dist/redevops-sidecar}"
[ -x "$BIN" ] || { echo "sidecar not found/executable: $BIN"; exit 2; }
W="$(mktemp -d)"; trap 'rm -f "$W"/*.out "$W"/*.err 2>/dev/null; rmdir "$W/home" "$W/tmp" "$W" 2>/dev/null' EXIT
mkdir -p "$W/home" "$W/tmp"

run() {  # name, then env assignments
  local name="$1"; shift
  env -i PATH=/usr/bin:/bin HOME="$W/home" TMPDIR="$W/tmp" RDO_DISABLE_PAIR=1 "$@" \
      "$BIN" bootstrap >"$W/$name.out" 2>"$W/$name.err" </dev/null
  echo $? >"$W/$name.rc"
}
LOCALES="$(locale -a 2>/dev/null || true)"
have_locale() { printf '%s\n' "$LOCALES" | grep -qix -- "$1"; }

run base LANG=en_US.UTF-8
BASE_SHA="$(shasum -a 256 <"$W/base.out" | cut -d' ' -f1)"; BASE_RC="$(cat "$W/base.rc")"
BASE_LEN="$(wc -c <"$W/base.out" | tr -d ' ')"
echo "baseline (LANG=en_US.UTF-8): rc=$BASE_RC bytes=$BASE_LEN sha256=$BASE_SHA"
[ "$BASE_LEN" -gt 0 ] || { echo "FAIL: baseline produced no output"; cat "$W/base.err"; exit 1; }
grep -q "device readiness" "$W/base.out" || { echo "FAIL: baseline lacks the report header"; exit 1; }

fail=0; n=0
check() {
  local name="$1"
  n=$((n+1))
  local sha rc len; sha="$(shasum -a 256 <"$W/$name.out" | cut -d' ' -f1)"; rc="$(cat "$W/$name.rc")"; len="$(wc -c <"$W/$name.out" | tr -d ' ')"
  if [ "$sha" = "$BASE_SHA" ] && [ "$rc" = "$BASE_RC" ] && ! grep -q UnicodeEncodeError "$W/$name.err"; then
    echo "ok    $name: rc=$rc bytes=$len"
  else
    echo "FAIL  $name: rc=$rc (want $BASE_RC) bytes=$len (want $BASE_LEN) $(grep -m1 UnicodeEncodeError "$W/$name.err")"; fail=1
  fi
}

for loc in en_US.ISO8859-1 en_US.US-ASCII ru_RU.CP1251 en_US.ISO8859-15; do
  if have_locale "$loc"; then run "loc-$loc" LC_ALL="$loc" LANG="$loc"; check "loc-$loc"
  else echo "skip  $loc (not installed)"; fi
done
run c-locale LC_ALL=C LANG=C;                                       check c-locale
run env-cp1252 LANG=en_US.UTF-8 PYTHONIOENCODING=cp1252;            check env-cp1252
run env-ascii LANG=en_US.UTF-8 PYTHONIOENCODING=ascii PYTHONUTF8=0; check env-ascii
if have_locale en_US.ISO8859-1; then
  run latin1-env LC_ALL=en_US.ISO8859-1 PYTHONIOENCODING=latin-1 PYTHONUTF8=0; check latin1-env
fi
echo "variants checked: $n, failures: $fail"
exit $fail
