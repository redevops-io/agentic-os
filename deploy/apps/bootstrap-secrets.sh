#!/usr/bin/env sh
# Idempotent secrets bootstrap for the self-host apps bundle.
#
# Delegates to the real credential broker (`python3 -m agentic_os.cli secrets init` -
# the SAME code as `agentic-os secrets init`) whenever a Python environment with
# `agentic_os` installed is reachable on PATH, so this script never re-implements
# backend selection, Vault/OpenBao handling, or the shell-profile opt-out - it reuses
# them. Backend is chosen with REDEVOPS_SECRETS_BACKEND (auto|file|vault; default auto:
# vault only if VAULT_ADDR+VAULT_TOKEN are set, else the local file - unchanged default
# behavior). Set REDEVOPS_SECRETS_NO_SHELL_PROFILE=1 to skip installing the managed
# ~/.zshrc / ~/.bashrc block (the broker's own opt-out, passed through unchanged).
#
# `--backend vault` requested explicitly but unreachable/misconfigured FAILS CLOSED
# (non-zero exit, nothing written anywhere) - it never silently falls back to the
# local file, which would be a silent security downgrade.
#
# Falls back to the original Python-free, local-file-only generator below when no
# such Python environment exists (e.g. bootstrapping a bare host before any Python
# install) - this script's "works without Python" guarantee stays a real fallback,
# not a removed capability, and this fallback path is unchanged from before: it only
# ever generates the local file, the same names/formats as `agentic-os secrets init
# --backend file`. Never prints or logs secret values, in either path.
set -eu

BACKEND="${REDEVOPS_SECRETS_BACKEND:-auto}"

find_python_with_agentic_os() {
  for cand in python3 python; do
    if command -v "$cand" >/dev/null 2>&1 && "$cand" -c "import agentic_os.secrets.bootstrap" >/dev/null 2>&1; then
      echo "$cand"
      return 0
    fi
  done
  return 1
}

PY="$(find_python_with_agentic_os || true)"

# This bundle's own database passwords and signing key (into ./.env), after the platform secrets.
bundle_passwords() { sh "$(dirname -- "$0")/bundle-passwords.sh"; }

if [ -n "$PY" ]; then
  set -- secrets init --backend "$BACKEND"
  [ "${REDEVOPS_SECRETS_NO_SHELL_PROFILE:-0}" = "1" ] && set -- "$@" --no-shell-profile
  "$PY" -m agentic_os.cli "$@"
  bundle_passwords
  echo "  next, in this directory: make up  (compose needs BOTH .env and the secrets file: --env-file .env --env-file <secrets file>)"
  exit 0
fi

if [ "$BACKEND" = "vault" ]; then
  echo "  error: REDEVOPS_SECRETS_BACKEND=vault needs a Python environment with agentic_os installed" >&2
  echo "         (no fallback to the local file - that would silently downgrade the requested backend)." >&2
  exit 1
fi

# `auto` means Vault when Vault is configured, and this fallback cannot write to Vault: writing the local file here
# would be the same silent downgrade. Saying `file` is how to ask for the local file on purpose.
if [ "$BACKEND" != "file" ] && [ -n "${VAULT_ADDR:-}" ] && [ -n "${VAULT_TOKEN:-}" ]; then
  echo "  error: VAULT_ADDR and VAULT_TOKEN are set, but writing to Vault needs a Python environment with agentic_os" >&2
  echo "         installed. Nothing was written. Install it, or set REDEVOPS_SECRETS_BACKEND=file to use a local file." >&2
  exit 1
fi

echo "  (no local agentic_os install found on PATH - using the local-file-only generator)"

# ---- fallback: Python-free, local-file-only generator (unchanged from before) ----
F="${REDEVOPS_SECRETS_FILE:-$HOME/.config/redevops/secrets.env}"
D="$(dirname "$F")"
[ -d "$D" ] || { mkdir -p "$D"; chmod 700 "$D"; }
umask 077
[ -f "$F" ] || : > "$F"
chmod 600 "$F"

have() { # $1=name ; true if env or file already has a non-empty value
  eval "v=\${$1:-}"
  [ -n "$v" ] && return 0
  grep -Eq "^$1=.+" "$F"
}
hex32() {
  if command -v openssl >/dev/null 2>&1; then openssl rand -hex 32
  else head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; fi
}
urlsafe24() { # 32 url-safe chars
  if command -v openssl >/dev/null 2>&1; then openssl rand -base64 32 | tr '+/' '-_' | tr -d '=\n' | cut -c1-32
  else head -c 24 /dev/urandom | base64 | tr '+/' '-_' | tr -d '=\n'; fi
}
gen() { # $1=name $2=value
  have "$1" && { echo "  $1: already-set"; return 0; }
  printf '%s=%s\n' "$1" "$2" >> "$F"
  echo "  $1: generated"
}
gen AGENTIC_OS_APPROVAL_SECRET "$(hex32)"
gen TWENTY_APP_SECRET "$(hex32)"
gen OUTREACH_BASIC_AUTH "operator:$(urlsafe24)"
echo "  secrets file: $F"
bundle_passwords
echo "  next: docker compose --env-file .env --env-file \"$F\" up -d"
