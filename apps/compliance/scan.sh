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
#   DOCKER         docker invocation (default: "sudo docker")
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTENT_DIR="$HERE/content"
RESULTS_DIR="$HERE/results"
DS="$CONTENT_DIR/ssg-ubuntu2204-ds.xml"

SCAP_PROFILE="${SCAP_PROFILE:-xccdf_org.ssgproject.content_profile_cis_level1_server}"
SCAP_IMAGE="${SCAP_IMAGE:-ubuntu:22.04}"
DOCKER="${DOCKER:-sudo docker}"

SSG_VERSION="0.1.81"
SSG_URL="https://github.com/ComplianceAsCode/content/releases/download/v${SSG_VERSION}/scap-security-guide-${SSG_VERSION}.tar.gz"

mkdir -p "$CONTENT_DIR" "$RESULTS_DIR"

# 1. Ensure the real SCAP content (datastream) is present. Fetch the official
#    ComplianceAsCode release and extract just the Ubuntu 22.04 datastream.
if [[ ! -f "$DS" ]]; then
  echo "[scan] SCAP content missing — fetching ComplianceAsCode SSG ${SSG_VERSION}..."
  TMP="$(mktemp -d)"
  curl -sL -o "$TMP/ssg.tar.gz" "$SSG_URL"
  tar xzf "$TMP/ssg.tar.gz" -C "$TMP" "scap-security-guide-${SSG_VERSION}/ssg-ubuntu2204-ds.xml"
  cp "$TMP/scap-security-guide-${SSG_VERSION}/ssg-ubuntu2204-ds.xml" "$DS"
  rm -rf "$TMP"
fi
echo "[scan] datastream: $DS ($(du -h "$DS" | cut -f1))"
echo "[scan] profile:    $SCAP_PROFILE"

# 2. Run the real oscap scan inside a disposable container. oscap installs from
#    Ubuntu's repos (libopenscap8 -> /usr/bin/oscap). Exit code 2 = scan ran but
#    some rules failed (expected, not an error for us); 1 = real failure.
set +e
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
rc=$?
set -e

if [[ "$rc" -gt 2 ]]; then
  echo "[scan] oscap failed (exit $rc)" >&2
  exit "$rc"
fi

if [[ ! -s "$RESULTS_DIR/scan-results.xml" ]]; then
  echo "[scan] no results file produced" >&2
  exit 1
fi

# 3. Quick summary from the real results.
echo "[scan] done (oscap exit $rc). Results:"
for r in pass fail notapplicable; do
  c=$(grep -oE "<result>$r</result>" "$RESULTS_DIR/scan-results.xml" | wc -l | tr -d ' ')
  printf "         %-14s %s\n" "$r" "$c"
done
echo "[scan] wrote $RESULTS_DIR/scan-results.xml + report.html"
