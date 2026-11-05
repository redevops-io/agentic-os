#!/bin/bash
# DEVELOPMENT ONLY. Runs packaging/notices/tools/dev_signer_e2e.py (a disposable-signer test of the About / Legal decision-signature
# mechanism) with an allowlisted environment: no SSH_AUTH_SOCK, no SSH_AGENT_PID, no credentials, a throwaway HOME. The key the harness
# creates lives in a temporary directory and is deleted before it exits. See docs/ABOUT_LEGAL_DEV_SIGNER.md.
#   PYTHON=/path/to/python3 packaging/notices/tools/dev_signer_e2e.py ...   (default: python3 from the allowlisted PATH)
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMPHOME="$(mktemp -d "${TMPDIR:-/tmp}/dev-signer-home.XXXXXX")" || exit 4
cleanup() { rmdir "$TMPHOME" 2>/dev/null; }
trap cleanup EXIT
PY="${PYTHON:-python3}"
env -i PATH="/usr/bin:/bin:/opt/homebrew/bin:$(dirname "$(command -v "$PY")")" HOME="$TMPHOME" TMPDIR="${TMPDIR:-/tmp}" LANG=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 "$(command -v "$PY")" "$HERE/dev_signer_e2e.py" "$@"
