#!/usr/bin/env sh
# Idempotent, Python-free secrets bootstrap (same names/formats as `agentic-os secrets init`).
# Writes ${REDEVOPS_SECRETS_FILE:-$HOME/.config/redevops/secrets.env} (0600). Never overwrites a value that is
# already in the environment or the file. Never prints values.
set -eu
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
echo "  next: docker compose --env-file \"$F\" up -d"
