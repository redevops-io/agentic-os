#!/bin/bash
# check_scope_parse.sh --src SRC_ROOT [--kind macos|windows|sshsig] [--no-window] <scope-file>   (release 0.6.0 copy of the c317 tool, with the windows kind added)
# Parse a scope file with the REAL r0p_scope_load / r0p_window_check of the pinned signing-common.sh and the key list / version of the script it is for
# (macos: sign_and_notarize_r0prime_macos_v3.sh, scope_version 3, 23 keys; sshsig: sign_manifest_sshsig.sh, scope_version 2, 11 keys), without running the signing
# script. No secret, no keychain, no network; the only thing written is a private temp directory (scope copy) that is removed by name. Prints "PARSE OK ..." and the
# scope sha256; exit 0 only if the file parses, its non-secret fields are well-formed and now is inside its window (unless --no-window).
set -u
SRC=""; SC=""; NOWIN=0; KIND=macos
while [ $# -gt 0 ]; do case "$1" in --src) SRC="$2"; shift 2;; --kind) KIND="$2"; shift 2;; --no-window) NOWIN=1; shift;; *) SC="$1"; shift;; esac; done
[ -d "$SRC/packaging/signing" ] && [ -f "$SC" ] || { echo "usage: check_scope_parse.sh --src SRC_ROOT [--kind macos|windows|sshsig] [--no-window] <scope-file>" >&2; exit 2; }
SELF_DIR="$(cd "$SRC/packaging/signing" && pwd -P)"
die() { echo "REFUSED: $*" >&2; exit 1; }
# shellcheck disable=SC1091
. "$SELF_DIR/signing-common.sh" || exit 2
AUTH="$(/usr/bin/shasum -a 256 "$SC" | /usr/bin/awk '{print $1}')"
r0p_mktmp
tidy() { [ -z "${R0P_TMP:-}" ] || { /bin/rm -f "$R0P_TMP/scope.copy"; /bin/rmdir "$R0P_TMP" 2>/dev/null; }; }
trap tidy EXIT
case "$KIND" in
  sshsig)
    r0p_scope_load "$SC" "$AUTH" scope_version not_before not_after script_sha256 common_sha256 identity_json_sha256 manifest_sha256 key_fingerprint namespace build_commit procedure_commit
    [ "$S_scope_version" = 2 ] || die "scope_version must be 2"
    [[ "$S_key_fingerprint" =~ ^SHA256:[A-Za-z0-9+/]{43}$ ]] || die "key_fingerprint format"
    [ "$S_namespace" = "redevops-qa-release@v1" ] || die "namespace must be redevops-qa-release@v1"
    [[ "$S_build_commit" =~ ^[0-9a-f]{40}$ && "$S_procedure_commit" =~ ^[0-9a-f]{40}$ ]] || die "commits must be 40 hex"
    for k in script_sha256 common_sha256 identity_json_sha256 manifest_sha256; do eval "v=\$S_$k"; [[ "$v" =~ ^[0-9a-f]{64}$ ]] || die "$k is not 64 hex"; done
    if [ "$NOWIN" = 0 ]; then r0p_window_check "$S_not_before" "$S_not_after"; fi
    echo "PARSE OK (sshsig): 11 keys, scope_version 2, window $S_not_before .. $S_not_after, manifest_sha256=$S_manifest_sha256, key $S_key_fingerprint"
    ;;
  macos)
    r0p_scope_load "$SC" "$AUTH" scope_version not_before not_after script_sha256 common_sha256 entitlements_sha256 \
      identity_json_sha256 input_dmg_sha256 identity_sha256 notary_profile_sha256 team_id notarize upload_to distribution \
      gate_sha256 gate_tool_sha256 evidence_manifest_sha256 build_commit procedure_commit \
      check_about_sha256 generate_about_sha256 about_record_sha256 legal_signers_sha256
    [ "$S_scope_version" = 3 ] || die "scope_version must be 3"
    [[ "$S_team_id" =~ ^[A-Z0-9]{10}$ ]] || die "team_id must be 10 characters [A-Z0-9]"
    [[ "$S_identity_sha256" =~ ^[0-9a-f]{64}$ ]] || die "identity_sha256 is not 64 hex"
    if [ "$S_notary_profile_sha256" != none ]; then [[ "$S_notary_profile_sha256" =~ ^[0-9a-f]{64}$ ]] || die "notary_profile_sha256 is not 64 hex or none"; fi
    for k in script_sha256 common_sha256 entitlements_sha256 identity_json_sha256 input_dmg_sha256 gate_sha256 gate_tool_sha256 evidence_manifest_sha256 check_about_sha256 generate_about_sha256 about_record_sha256 legal_signers_sha256; do
      eval "v=\$S_$k"; [[ "$v" =~ ^[0-9a-f]{64}$ ]] || die "$k is not 64 hex"; done
    [[ "$S_build_commit" =~ ^[0-9a-f]{40}$ && "$S_procedure_commit" =~ ^[0-9a-f]{40}$ ]] || die "build_commit/procedure_commit must be 40 hex"
    if [ "$NOWIN" = 0 ]; then r0p_window_check "$S_not_before" "$S_not_after"; fi
    echo "PARSE OK: 23 keys, scope_version 3, window $S_not_before .. $S_not_after, notarize=$S_notarize upload_to=$S_upload_to distribution=$S_distribution team_id=$S_team_id"
    ;;
  windows)
    r0p_scope_load "$SC" "$AUTH" scope_version not_before not_after script_sha256 common_sha256 verifier_sha256 identity_json_sha256 \
      stage inputs_sha256 az_endpoint az_account az_profile expected_cn gate_sha256 gate_tool_sha256 distribution evidence_manifest_sha256 \
      build_commit procedure_commit tool_az_sha256 tool_java_sha256 tool_python_sha256 tool_python_packages_sha256 tool_python_pth_sha256 tool_osslsigncode_sha256 container_listing_sha256 \
      check_about_sha256 generate_about_sha256 about_record_sha256 legal_signers_sha256
    [ "$S_scope_version" = 3 ] || die "scope_version must be 3"
    case "$S_stage" in inner|container) ;; *) die "stage must be inner or container";; esac
    [[ "$S_az_endpoint" =~ ^https://[a-z0-9]+\.codesigning\.azure\.net/?$ ]] || die "az_endpoint format"
    [[ "$S_az_account" =~ ^[A-Za-z0-9-]{3,63}$ && "$S_az_profile" =~ ^[A-Za-z0-9-]{3,63}$ ]] || die "az_account/az_profile format"
    case "$S_distribution" in yes|no) ;; *) die "distribution must be yes or no";; esac
    [[ "$S_build_commit" =~ ^[0-9a-f]{40}$ && "$S_procedure_commit" =~ ^[0-9a-f]{40}$ ]] || die "build_commit/procedure_commit must be 40 hex"
    for k in script_sha256 common_sha256 verifier_sha256 identity_json_sha256 gate_sha256 gate_tool_sha256 evidence_manifest_sha256 check_about_sha256 generate_about_sha256 about_record_sha256 legal_signers_sha256 \
             tool_az_sha256 tool_java_sha256 tool_python_sha256 tool_python_packages_sha256 tool_python_pth_sha256; do
      eval "v=\$S_$k"; [[ "$v" =~ ^[0-9a-f]{64}$ ]] || die "$k is not 64 hex"; done
    if [ "$S_tool_osslsigncode_sha256" != none ]; then [[ "$S_tool_osslsigncode_sha256" =~ ^[0-9a-f]{64}$ ]] || die "tool_osslsigncode_sha256 is not 64 hex or none"; fi
    if [ "$S_stage" = container ]; then [[ "$S_container_listing_sha256" =~ ^[0-9a-f]{64}$ ]] || die "container_listing_sha256 must be 64 hex for stage container"
    else [ "$S_container_listing_sha256" = none ] || die "container_listing_sha256 must be none for stage inner"; fi
    if [ "$NOWIN" = 0 ]; then r0p_window_check "$S_not_before" "$S_not_after"; fi
    echo "PARSE OK (windows): 30 keys, scope_version 3, stage $S_stage, window $S_not_before .. $S_not_after, distribution=$S_distribution"
    ;;
  *) die "--kind must be macos, windows or sshsig";;
esac
echo "scope sha256 = $AUTH"
tidy
