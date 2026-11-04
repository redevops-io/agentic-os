#!/usr/bin/env bash
# r0prime_win_signcommand.sh  (version r0prime-handoff-1)
#
# Hand-off helper for the WINDOWS BUILD HOST. It is the `signCommand` that tauri-bundler calls for every file it would sign
# (bundle.windows.signCommand = {"cmd":"bash","args":["<abs path>/r0prime_win_signcommand.sh","%1"]}). It holds NO credential,
# contacts NO service and signs NOTHING: it only copies files out (collect) or puts files that were signed ELSEWHERE (on the signing
# host, by sign_windows_r0prime_v3.sh) back in (apply). So the build host needs no secret and the repository holds none.
#
# Environment (set by the person running the build):
#   R0P_HANDOFF_MODE  collect | apply
#   R0P_HANDOFF_DIR   an existing ABSOLUTE directory for this round (kept outside the source tree)
#
# Layout inside R0P_HANDOFF_DIR:
#   requests-inner/<role>-<sha256 of the unsigned file>.exe   written by collect (and apply for unknown files is an error)
#   requests-container/<installer file name>                  the installers as the bundler hands them over (UNSIGNED)
#   signed-inner/<role>-<sha256>.exe + signed-inner/SHA256SUMS   put here by the owner: the signed files and the SHA256SUMS of the
#                                                                signing run (sign_windows_r0prime_v3.sh --stage inner)
#   signcommand.log                                            one line per call (time, mode, role, sha256, action); no secrets
#
# Roles by file name: agentic-r0prime.exe -> launcher; redevops-sidecar.exe / redevops-sidecar-*.exe -> sidecar; uninstall*.exe /
# *uninstaller*.exe -> uninstaller; *.msi and *-setup.exe -> container; every .dll (NSIS plugins, WiX extensions, ...) -> SKIPPED:
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
case "$LB" in
  *.dll) ROLE=skip;;
  *.msi) ROLE=container;;
  *-setup.exe) ROLE=container;;
  agentic-r0prime.exe) ROLE=launcher;;
  redevops-sidecar.exe|redevops-sidecar-*.exe) ROLE=sidecar;;
  uninstall*.exe|*uninstaller*.exe) ROLE=uninstaller;;
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
  put_request requests-inner "$REQ"; logline "collected inner request $REQ (file left unchanged)"; echo "collected: $REQ"; exit 0
fi

# ---- apply (inner roles only)
[ "$ROLE" != unknown ] || { logline "FAIL unknown role"; die "unknown executable $B: refusing to let it into the installer unsigned (add its role after review, or remove it)"; }
SIGNED_DIR="$DIR/signed-inner"
[ -f "$SIGNED_DIR/SHA256SUMS" ] || { logline "FAIL no SHA256SUMS"; die "signed-inner/SHA256SUMS is missing (copy the SHA256SUMS of the signing run next to the signed files)"; }
# already one of the signed files? (idempotent: the sidecar is signed in place and the bundler may ask again)
while IFS= read -r line || [ -n "$line" ]; do
  want="${line%% *}"; name="${line#*  }"
  if [ "$want" = "$SHA" ]; then logline "already the signed file $name (accepted unchanged)"; echo "already signed: $name"; exit 0; fi
done < "$SIGNED_DIR/SHA256SUMS"
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
cp "$CAND" "$F"
[ "$(hash_file "$F")" = "$CSHA" ] || die "the replacement did not land (hash differs after copy)"
logline "replaced by signed counterpart $REQ (sha256 $CSHA)"
echo "replaced: $B <- $REQ"
