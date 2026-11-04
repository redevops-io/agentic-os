#!/usr/bin/env bash
# Stage the frozen signed installers + Verify-FrozenSignatures.ps1 for an AUTHORIZED Windows host.
# Default is a DRY RUN: it checks the hashes of the local copies against the frozen baseline and PRINTS what to do.
# With --copy USER@HOST it runs scp (your own ssh config / agent; this script never reads keys or credentials itself).
#
#   ART_DIR=<dir with copies> ./stage-for-windows-host.sh        # dry run, prints instructions
#   ./stage-for-windows-host.sh --copy tester@win11-host         # scp the 4 files to the host's temp dir
#   ./stage-for-windows-host.sh --copy tester@win11-host --dest 'C:/Users/tester/frozen-check'
#
# Works in bash and zsh (invoke as: bash stage-for-windows-host.sh ...).
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
ART=${ART_DIR:?set ART_DIR to a directory holding COPIES of the frozen .msi/.exe (and their SHA256SUMS); never the frozen directory itself}
PS1=$HERE/../Verify-FrozenSignatures.ps1
MSI=Agentic.Apps_0.1.0_x64_en-US.msi; EXE=Agentic.Apps_0.1.0_x64-setup.exe   # the frozen v0.3.2 file names
WANT_MSI=9e5e6262f7830255333060193cc4c285fcf60a72ee70905f22792c12e12d58e5
WANT_EXE=5931b1914e732c7b8106fc19f26e63e806fd1b0f3b876b2236cd2e24410fde23
DO_COPY=""; DEST='C:/Users/Public/frozen-check'
while [ $# -gt 0 ]; do
  case "$1" in
    --copy) DO_COPY=${2:?need USER@HOST}; shift 2;;
    --dest) DEST=${2:?need a path}; shift 2;;
    -h|--help) sed -n 2,14p "$0"; exit 0;;
    *) echo "unknown option $1" >&2; exit 2;;
  esac
done
sha() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }
ok=1
for p in "$MSI:$WANT_MSI" "$EXE:$WANT_EXE"; do
  f=${p%%:*}; w=${p##*:}
  [ -f "$ART/$f" ] || { echo "MISSING $ART/$f (set ART_DIR=<dir with COPIES of the frozen files>)"; ok=0; continue; }
  g=$(sha "$ART/$f"); if [ "$g" = "$w" ]; then echo "OK    $g  $f"; else echo "FAIL  $g  $f (expected $w)"; ok=0; fi
done
[ -f "$PS1" ] || { echo "MISSING $PS1"; ok=0; }
[ "$ok" = 1 ] || { echo "refusing to continue: local copies do not match the frozen baseline"; exit 1; }
echo "script sha256: $(sha "$PS1")  Verify-FrozenSignatures.ps1"
cat <<TXT

WHEN: run the check on or after 2026-11-07T03:13:00Z (leaf certificate NotAfter 2026-11-07T03:12:51Z). Earlier runs are informational only.
WHERE: only a Windows host you are authorized to use (a standard user account is enough; no admin rights, nothing is installed).
HOW to get the files there (any of):
  1. scp (this script can do it):   $0 --copy USER@HOST [--dest 'C:/Users/USER/frozen-check']
       (needs the Windows OpenSSH server on the host; the destination directory must exist)
  2. SMB / RDP drive redirection / USB: copy $MSI, $EXE, SHA256SUMS and Verify-FrozenSignatures.ps1 into one folder.
  After copying, in PowerShell on the host (folder = the destination):
       Get-FileHash .\\$MSI, .\\$EXE -Algorithm SHA256        # must equal the hashes above
       powershell -NoProfile -ExecutionPolicy Bypass -File .\\Verify-FrozenSignatures.ps1 -MsiPath .\\$MSI -ExePath .\\$EXE
     (-ExecutionPolicy Bypass applies to that one process only; no system setting changes.)
  Bring back: the output directory printed at the end (transcript.txt, transcript.sha256.txt, *.certutil.txt, *.cer).
TXT
if [ -n "$DO_COPY" ]; then
  echo; echo "== copying to $DO_COPY:$DEST =="
  for f in "$ART/$MSI" "$ART/$EXE" "$ART/SHA256SUMS" "$PS1"; do
    [ -f "$f" ] || continue
    scp -p -- "$f" "$DO_COPY:$DEST/" || { echo "scp failed for $f"; exit 1; }
  done
  echo "copied. Now run the PowerShell command above on the host."
fi
