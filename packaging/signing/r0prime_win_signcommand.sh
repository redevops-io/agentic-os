#!/usr/bin/env bash
# r0prime_win_signcommand.sh  (version r0prime-handoff-2)
#
# Hand-off helper for the WINDOWS BUILD HOST. It is the `signCommand` that tauri-bundler calls for every file it would sign
# (bundle.windows.signCommand = {"cmd":"bash","args":["<abs path>/r0prime_win_signcommand.sh","%1"]}). It holds NO credential,
# contacts NO service and signs NOTHING: it only copies files out (collect) or puts files that were signed ELSEWHERE (on the signing
# host, by sign_windows_r0prime_v3.sh) back in (apply). So the build host needs no secret and the repository holds none.
#
# Environment (set by the person running the build):
#   R0P_HANDOFF_MODE  collect | apply
#   R0P_HANDOFF_DIR   an existing ABSOLUTE directory for this round (kept outside the source tree; keep it private to the build user)
#   apply only (round 4, R4-07):
#   R0P_HANDOFF_REQUESTS      absolute path of the REQUESTS.sha256 that THIS build host wrote in its own collect round: an inner file is
#                             accepted only if its unsigned hash is in that list (the build host's own record), never merely because
#                             a SHA256SUMS next to the signed files says so
#   R0P_EXPECT_SIGNER_CN      exact first CN of the signer certificate that every signed inner file must carry (from the authorization)
#   R0P_EXPECT_ISSUER_SUBSTR  text the signer certificate's issuer must contain (from the authorization)
#   The signature of every signed file is checked with powershell.exe Get-AuthenticodeSignature (Status Valid, signer CN, issuer text,
#   a timestamper certificate present) BEFORE the file is copied in; no check, no replacement. powershell.exe is taken from %SystemRoot%\System32\
#   (absolute path from cygpath -S, not PATH or SYSTEMROOT; no override in this file). (UNVERIFIED on a real Windows host.)
#
# Layout inside R0P_HANDOFF_DIR:
#   requests-inner/<role>-<sha256 of the unsigned file>.exe   written by collect (and apply for unknown files is an error)
#   requests-container/<installer file name>                  the installers as the bundler hands them over (UNSIGNED)
#   signed-inner/<role>-<sha256>.exe + signed-inner/SHA256SUMS   put here by the owner: the signed files and the SHA256SUMS of the
#                                                                signing run (sign_windows_r0prime_v3.sh --stage inner)
#   signcommand.log                                            one line per call (time, mode, role, sha256, action); no secrets
#
# Roles by file name: agentic-r0prime.exe -> launcher; redevops-sidecar.exe / redevops-sidecar-*.exe -> sidecar; uninstall*.exe /
# *uninstaller*.exe / nstXXXX.tmp (the NSIS uninstaller as makensis passes it) -> uninstaller; *.msi and *-setup.exe -> container; every .dll (NSIS plugins, WiX extensions, ...) -> SKIPPED:
# third-party files are NEVER signed with the release identity. Any other .exe has role 'unknown': collect records it (the owner
# decides), apply FAILS the build.
#
# collect: copies each inner file to requests-inner/ and each container to requests-container/ and returns success WITHOUT
#          modifying the file (the build continues with unsigned files; those installers are not deliverables).
# apply:   an INNER file is replaced by its signed counterpart if (and only if) signed-inner/<role>-<sha256 of the incoming file>.exe
#          exists and its sha256 equals the one listed in signed-inner/SHA256SUMS; a file that already IS a listed signed file is
#          accepted unchanged; anything else FAILS the build (it never lets an unsigned inner file through). A CONTAINER is copied
#          to requests-container/ and left unsigned (it is signed last, on the signing host, stage 'container').
#
# Why by hash: the bundler patches the launcher in place per package type before it signs it (tauri-bundler 2.9.4, STATIC), so the
# bytes handed over differ per installer type and cannot be signed ahead of time; the lookup only works if the SAME unsigned
# bytes appear again (same target dir, no rebuild between the rounds). A miss fails closed.
#
# bash 3.2 / Git Bash compatible. No network, no rm, no secrets.
case "$-" in *x*) echo "r0prime_win_signcommand: xtrace is on; refusing" >&2; exit 1;; esac
set +x
set -euo pipefail
umask 077

die() { echo "r0prime_win_signcommand: $*" >&2; exit 1; }
[ $# -eq 1 ] || die "usage: r0prime_win_signcommand.sh <file>   (called by tauri-bundler with %1)"
F="$1"
MODE="${R0P_HANDOFF_MODE:-}"; DIR="${R0P_HANDOFF_DIR:-}"
case "$MODE" in collect|apply) ;; *) die "R0P_HANDOFF_MODE must be collect or apply";; esac
case "$DIR" in /*|[A-Za-z]:[/\\]*) ;; *) die "R0P_HANDOFF_DIR must be an absolute path";; esac
if command -v cygpath >/dev/null 2>&1; then DIR="$(cygpath -u "$DIR")"; F="$(cygpath -u "$F")"; fi
[ -d "$DIR" ] || die "R0P_HANDOFF_DIR is not an existing directory"
[ -f "$F" ] && [ ! -L "$F" ] || die "not a regular file: $F"

hash_file() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | cut -d' ' -f1
  else die "no sha256 tool (sha256sum or shasum) found"; fi
}
lower() { printf '%s' "$1" | tr 'A-Z' 'a-z'; }

B="$(basename "$F")"; LB="$(lower "$B")"; SHA="$(hash_file "$F")"
case "$B" in *[[:cntrl:]]*) die "file name with a control character; refusing (it could forge log lines)";; esac
case "$LB" in
  *.dll) ROLE=skip;;
  *.msi) ROLE=container;;
  *-setup.exe) ROLE=container;;
  agentic-r0prime.exe) ROLE=launcher;;
  redevops-sidecar.exe|redevops-sidecar-*.exe) ROLE=sidecar;;
  uninstall*.exe|*uninstaller*.exe) ROLE=uninstaller;;
  nst[0-9A-Fa-f]*.tmp) ROLE=uninstaller;;   # makensis hands !uninstfinalize the generated uninstaller as nstXXXX.tmp; without this the build ships it UNSIGNED
  *.exe) ROLE=unknown;;
  *) die "unexpected file type for signing: $B";;
esac
logline() { printf '%s mode=%s role=%s sha256=%s name=%s action=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$MODE" "$ROLE" "$SHA" "$B" "$1" >>"$DIR/signcommand.log"; }

if [ "$ROLE" = skip ]; then logline "skipped (third-party file: never signed with the release identity)"; echo "skip: $B"; exit 0; fi

put_request() { # subdir name
  local d="$DIR/$1" n="$2"
  mkdir -p "$d"
  if [ -e "$d/$n" ]; then
    [ "$(hash_file "$d/$n")" = "$SHA" ] || die "$1/$n already exists with different bytes; use a new R0P_HANDOFF_DIR for each round"
  else
    cp "$F" "$d/$n"
  fi
}

case "$ROLE" in
  container)
    put_request requests-container "$B"; logline "recorded container request (left unsigned)"; echo "container recorded: $B"; exit 0;;
esac

EXT="exe"
REQ="$ROLE-$SHA.$EXT"
if [ "$MODE" = collect ]; then
  put_request requests-inner "$REQ"
  # the build host's own record of what it asked to have signed (used by apply)
  touch "$DIR/REQUESTS.sha256"
  grep -q -F "  $REQ" "$DIR/REQUESTS.sha256" || printf '%s  %s\n' "$SHA" "$REQ" >>"$DIR/REQUESTS.sha256"
  logline "collected inner request $REQ (file left unchanged)"; echo "collected: $REQ"; exit 0
fi

# ---- apply (inner roles only)
[ "$ROLE" != unknown ] || { logline "FAIL unknown role"; die "unknown executable $B: refusing to let it into the installer unsigned (add its role after review, or remove it)"; }
REQLIST="${R0P_HANDOFF_REQUESTS:-}"
case "$REQLIST" in /*|[A-Za-z]:[/\\]*) ;; *) logline "FAIL no request list"; die "apply needs R0P_HANDOFF_REQUESTS (absolute path of the REQUESTS.sha256 this host wrote in its collect round)";; esac
if command -v cygpath >/dev/null 2>&1; then REQLIST="$(cygpath -u "$REQLIST")"; fi
[ -f "$REQLIST" ] && [ ! -L "$REQLIST" ] || die "R0P_HANDOFF_REQUESTS is not a regular file"
in_requests() { grep -q -F -x "$1  $2" "$REQLIST"; }   # unsigned-sha  request-name
SIGNED_DIR="$DIR/signed-inner"
[ -f "$SIGNED_DIR/SHA256SUMS" ] || { logline "FAIL no SHA256SUMS"; die "signed-inner/SHA256SUMS is missing (copy the SHA256SUMS of the signing run next to the signed files)"; }
# already one of the signed files? (idempotent: the sidecar is signed in place and the bundler may ask again)
while IFS= read -r line || [ -n "$line" ]; do
  want="${line%% *}"; name="${line#*  }"
  if [ "$want" = "$SHA" ]; then
    # the signed file must belong to a request this host recorded: name = <role>-<unsigned sha>.exe
    usha="${name#*-}"; usha="${usha%.exe}"
    in_requests "$usha" "$name" || { logline "FAIL signed file not from this host's requests"; die "$name is not one of the requests this build host recorded (R0P_HANDOFF_REQUESTS)"; }
    logline "already the signed file $name (accepted unchanged)"; echo "already signed: $name"; exit 0
  fi
done < "$SIGNED_DIR/SHA256SUMS"
in_requests "$SHA" "$REQ" || { logline "FAIL not in this host's request list"; die "$B (sha256 $SHA) is not in the request list this build host recorded in its collect round: refusing"; }
CAND="$SIGNED_DIR/$REQ"
[ -f "$CAND" ] && [ ! -L "$CAND" ] || { logline "FAIL no signed counterpart $REQ"; die "no signed counterpart for $B ($REQ): not in signed-inner/. The build must fail rather than ship an unsigned $ROLE"; }
EXPECT=""
while IFS= read -r line || [ -n "$line" ]; do
  name="${line#*  }"
  if [ "$name" = "$REQ" ]; then EXPECT="${line%% *}"; fi
done < "$SIGNED_DIR/SHA256SUMS"
[ -n "$EXPECT" ] || { logline "FAIL $REQ not listed"; die "$REQ is not listed in signed-inner/SHA256SUMS"; }
CSHA="$(hash_file "$CAND")"
[ "$CSHA" = "$EXPECT" ] || { logline "FAIL hash mismatch for $REQ"; die "signed-inner/$REQ does not match its SHA256SUMS entry"; }
[ "$CSHA" != "$SHA" ] || { logline "FAIL signed file equals unsigned file"; die "signed-inner/$REQ is byte-identical to the unsigned file: it is not signed"; }
# the signature identity of the signed counterpart, checked before it is accepted (Windows PowerShell on the build host)
[ -n "${R0P_EXPECT_SIGNER_CN:-}" ] && [ -n "${R0P_EXPECT_ISSUER_SUBSTR:-}" ] || { logline "FAIL no signer expectation"; die "apply needs R0P_EXPECT_SIGNER_CN and R0P_EXPECT_ISSUER_SUBSTR"; }
case "$CAND" in *[!A-Za-z0-9._/:\\\ -]*) die "unsafe characters in the signed file path";; esac
WINCAND="$CAND"; if command -v cygpath >/dev/null 2>&1; then WINCAND="$(cygpath -w "$CAND")"; fi
# powershell.exe is resolved to its absolute system path from the Windows system directory (cygpath -S), never through PATH or SYSTEMROOT. There is NO
# environment override in this shipped file (round 6, R6-04): the unit tests run a copy whose next line is replaced (R0P-TEST-PATCH-POINT-PS).
PS_EXE=""   # R0P-TEST-PATCH-POINT-PS
if [ -z "$PS_EXE" ]; then
  if command -v cygpath >/dev/null 2>&1; then PS_EXE="$(cygpath -u "$(cygpath -S)")/WindowsPowerShell/v1.0/powershell.exe"
  else logline "FAIL no powershell path"; die "cannot locate powershell.exe in the Windows system directory (cygpath -S is unavailable)"; fi
fi
case "$PS_EXE" in /*) ;; *) die "powershell.exe path is not absolute";; esac
[ -x "$PS_EXE" ] || { logline "FAIL powershell not executable"; die "powershell.exe not found at $PS_EXE"; }
SIGINFO="$("$PS_EXE" -NoProfile -NonInteractive -Command "\$s = Get-AuthenticodeSignature -LiteralPath '$WINCAND'; \"\$(\$s.Status)|\$(\$s.SignerCertificate.Subject)|\$(\$s.SignerCertificate.Issuer)|\$(\$s.TimeStamperCertificate -ne \$null)\"" 2>/dev/null | tr -d '\r')" || { logline "FAIL signature check could not run"; die "could not run powershell.exe Get-AuthenticodeSignature"; }
SIG_STATUS="${SIGINFO%%|*}"; REST="${SIGINFO#*|}"; SIG_SUBJECT="${REST%%|*}"; REST="${REST#*|}"; SIG_ISSUER="${REST%%|*}"; SIG_TS="${REST#*|}"
[ "$SIG_STATUS" = Valid ] || { logline "FAIL signature status $SIG_STATUS"; die "signed counterpart $REQ: signature status is '$SIG_STATUS', not Valid"; }
case "$SIG_SUBJECT" in "CN=$R0P_EXPECT_SIGNER_CN,"*|"CN=$R0P_EXPECT_SIGNER_CN") ;; *) logline "FAIL signer subject"; die "signed counterpart $REQ: signer subject is not CN=$R0P_EXPECT_SIGNER_CN";; esac
case "$SIG_ISSUER" in *"$R0P_EXPECT_ISSUER_SUBSTR"*) ;; *) logline "FAIL issuer"; die "signed counterpart $REQ: issuer does not contain the expected text";; esac
[ "$SIG_TS" = True ] || { logline "FAIL no timestamp"; die "signed counterpart $REQ carries no timestamp certificate"; }
cp "$CAND" "$F"
[ "$(hash_file "$F")" = "$CSHA" ] || die "the replacement did not land (hash differs after copy)"
logline "replaced by signed counterpart $REQ (sha256 $CSHA)"
echo "replaced: $B <- $REQ"
