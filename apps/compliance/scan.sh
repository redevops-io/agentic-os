#!/usr/bin/env bash
# scan.sh — run a REAL OpenSCAP (oscap) scan for the agentic-compliance module.
#
# Produces real pass/fail rule results that app.py parses and the agent monitors.
# Self-contained: no cloud creds. The scan evaluates the throwaway scanner
# container against the CIS Ubuntu 22.04 Level 1 - Server benchmark using the
# ComplianceAsCode SCAP Security Guide datastream (ssg-ubuntu2204-ds.xml).
#
# Outputs (in agents/compliance/results/):
#   scan-results.xml   real XCCDF results (rule id, title, result, severity)
#   report.html        the human-readable OpenSCAP HTML report (served at /report)
#
# Idempotent: re-running re-scans and overwrites the results. app.py caches the
# parsed results; POST /agent/run {"action":"scan"} shells out to this script.
#
# Env knobs:
#   SCAP_PROFILE   xccdf profile id (default: CIS Level 1 Server)
#   SCAP_IMAGE     base image to scan/scan-from (default: ubuntu:22.04)
#   DOCKER         docker invocation (default: "docker"; if your user cannot reach the docker socket,
#                  set DOCKER="sudo docker" yourself - this script never adds sudo on its own)
#   SCAN_MODE      "container" (default): evaluate the pristine SCAP_IMAGE in a throwaway container. This
#                  scans the IMAGE, NOT the host. "host": run the host's own oscap directly (needs oscap
#                  installed; no container, no sudo) - only this mode scans the machine it runs on.
#   COMPLIANCE_ALLOW_UNPINNED_SSG  "true" to skip SSG tarball hash verification (default: fail closed)
#
# Exit codes: 0 = scan passed, 2 = valid scan with failing rules, anything else = error (oscap 1 = error).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTENT_DIR="$HERE/content"
RESULTS_DIR="$HERE/results"
DS="$CONTENT_DIR/ssg-ubuntu2204-ds.xml"

SCAP_PROFILE="${SCAP_PROFILE:-xccdf_org.ssgproject.content_profile_cis_level1_server}"
SCAP_IMAGE="${SCAP_IMAGE:-ubuntu:22.04}"
DOCKER="${DOCKER:-docker}"
SCAN_MODE="${SCAN_MODE:-container}"

SSG_VERSION="0.1.81"
SSG_URL="https://github.com/ComplianceAsCode/content/releases/download/v${SSG_VERSION}/scap-security-guide-${SSG_VERSION}.tar.gz"

SSG_HASH_FILE="${SSG_HASH_FILE:-$HERE/env/ssg-${SSG_VERSION}.sha256}"

SSG_VERIFIED_FILE="$CONTENT_DIR/ssg-verified.json"   # what the datastream's source tarball was verified against

mkdir -p "$CONTENT_DIR" "$RESULTS_DIR"

# 1. Ensure the real SCAP content (datastream) is present. Fetch the official
#    ComplianceAsCode release and extract just the Ubuntu 22.04 datastream.
if [[ ! -f "$DS" ]]; then
  echo "[scan] SCAP content missing — fetching ComplianceAsCode SSG ${SSG_VERSION}..."
  TMP="$(mktemp -d)"
  curl -fsSL -o "$TMP/ssg.tar.gz" "$SSG_URL"
  EXPECTED=""
  SSG_STATE="unpinned"; ACTUAL="$(sha256sum "$TMP/ssg.tar.gz" | awk '{print $1}')"
  [[ -f "$SSG_HASH_FILE" ]] && EXPECTED="$(grep -vE '^\s*#' "$SSG_HASH_FILE" | awk 'NF{print $1; exit}')"
  if [[ -z "$EXPECTED" ]]; then
    if [[ "${COMPLIANCE_ALLOW_UNPINNED_SSG:-}" == "true" ]]; then
      echo "[scan] WARNING: no pinned SSG hash at $SSG_HASH_FILE; continuing UNVERIFIED (COMPLIANCE_ALLOW_UNPINNED_SSG=true)" >&2
    else
      echo "[scan] ERROR: no pinned sha256 for the SSG tarball ($SSG_HASH_FILE); refusing to use unverified content. Set COMPLIANCE_ALLOW_UNPINNED_SSG=true to override." >&2
      rm -rf "$TMP"; exit 3
    fi
  else
    SSG_STATE="verified"
    if [[ "$ACTUAL" != "$EXPECTED" ]]; then
      SSG_STATE="mismatch-overridden"
      if [[ "${COMPLIANCE_ALLOW_UNPINNED_SSG:-}" == "true" ]]; then
        echo "[scan] WARNING: SSG sha256 mismatch (expected $EXPECTED got $ACTUAL); continuing (COMPLIANCE_ALLOW_UNPINNED_SSG=true)" >&2
      else
        echo "[scan] ERROR: SSG tarball sha256 mismatch (expected $EXPECTED, got $ACTUAL); refusing to use it." >&2
        rm -rf "$TMP"; exit 3
      fi
    fi
  fi
  tar xzf "$TMP/ssg.tar.gz" -C "$TMP" "scap-security-guide-${SSG_VERSION}/ssg-ubuntu2204-ds.xml"
  cp "$TMP/scap-security-guide-${SSG_VERSION}/ssg-ubuntu2204-ds.xml" "$DS"
  printf '{"ssg_version":"%s","ssg_tarball_sha256":"%s","ssg_hash_status":"%s"}\n' \
    "$SSG_VERSION" "$ACTUAL" "$SSG_STATE" > "$SSG_VERIFIED_FILE"
  rm -rf "$TMP"
fi
echo "[scan] datastream: $DS ($(du -h "$DS" | cut -f1))"
echo "[scan] profile:    $SCAP_PROFILE"

# What SSG source was verified (recorded at fetch time; "unknown" if the datastream was supplied by other means).
SSG_JSON='{"ssg_version":"unknown","ssg_tarball_sha256":null,"ssg_hash_status":"unknown"}'
[[ -s "$SSG_VERIFIED_FILE" ]] && SSG_JSON="$(tr -d '\n' < "$SSG_VERIFIED_FILE")"
SSG_FIELDS="${SSG_JSON#\{}"; SSG_FIELDS="${SSG_FIELDS%\}}"

# 2. Run the real oscap scan. oscap exit codes: 0 = all rules pass, 2 = scan ran but some rules failed
#    (a valid scan WITH findings), 1 = error, anything else = error.
set +e
rm -f "$RESULTS_DIR/scan-results.xml" "$RESULTS_DIR/report.html"
if [[ "$SCAN_MODE" == "host" ]]; then
  TARGET_JSON="{\"target\":\"host\",\"host_scanned\":true,\"hostname\":\"$(hostname)\",${SSG_FIELDS}}"
  oscap xccdf eval --profile "$SCAP_PROFILE" --results "$RESULTS_DIR/scan-results.xml" \
    --report "$RESULTS_DIR/report.html" "$DS"
else
  TARGET_JSON="{\"target\":\"container-image\",\"image\":\"$SCAP_IMAGE\",\"host_scanned\":false,${SSG_FIELDS}}"
  # Evaluates the pristine image in a disposable container - NOT the host this runs on.
  $DOCKER run --rm \
    -v "$CONTENT_DIR":/content:ro \
    -v "$RESULTS_DIR":/results \
    "$SCAP_IMAGE" bash -lc "
      apt-get update -qq >/dev/null 2>&1 &&
      apt-get install -y -qq libopenscap8 >/dev/null 2>&1 &&
      oscap xccdf eval \
        --profile '$SCAP_PROFILE' \
        --results /results/scan-results.xml \
        --report  /results/report.html \
        /content/ssg-ubuntu2204-ds.xml
    "
fi
rc=$?
set -e

if [[ "$rc" -ne 0 && "$rc" -ne 2 ]]; then
  echo "[scan] oscap failed (exit $rc; 1 = error, only 0 and 2 are valid scans)" >&2
  exit "$rc"
fi

if [[ ! -s "$RESULTS_DIR/scan-results.xml" ]]; then
  echo "[scan] no results file produced" >&2
  exit 1
fi

echo "$TARGET_JSON" > "$RESULTS_DIR/scan-target.json"

# 3. Quick summary from the real results.
echo "[scan] done (oscap exit $rc). Results:"
for r in pass fail notapplicable; do
  c=$(grep -oE "<result>$r</result>" "$RESULTS_DIR/scan-results.xml" | wc -l | tr -d ' ')
  printf "         %-14s %s\n" "$r" "$c"
done
echo "[scan] wrote $RESULTS_DIR/scan-results.xml + report.html"
echo "[scan] target: $TARGET_JSON"
exit "$rc"
