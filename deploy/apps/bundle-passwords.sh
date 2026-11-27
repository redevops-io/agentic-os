#!/usr/bin/env sh
# This bundle's own passwords: Postiz's signing key and the Twenty, Metabase and Postiz database passwords.
# compose.yml has no default for any of them, so every installation gets its own. Run by ./bootstrap-secrets.sh.
#
# They are written into this directory's .env (what `docker compose` reads), and ONLY where there is none yet:
# an existing value is never replaced. A database keeps the password its volume was created with, so none is generated
# for a database whose volume already exists, or when Docker cannot be asked. Values are never printed.
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
F="${REDEVOPS_BUNDLE_ENV:-$here/.env}"
SECRETS="${REDEVOPS_SECRETS_FILE:-$HOME/.config/redevops/secrets.env}"

umask 077
if [ ! -f "$F" ]; then
  if [ -f "$here/.env.example" ]; then cp "$here/.env.example" "$F"; else : > "$F"; fi
  echo "  created $F"
fi
chmod 600 "$F"

in_file() { # $1=file $2=name ; the last non-empty value, or nothing
  [ -f "$1" ] || return 0
  sed -n "s/^$2=//p" "$1" | grep -v '^[[:space:]]*$' | tail -n 1 || true
}
current() { # $1=name ; the value compose would use, from the environment, .env or the secrets file
  eval "cur=\${$1:-}"
  [ -n "$cur" ] || cur=$(in_file "$F" "$1")
  [ -n "$cur" ] || cur=$(in_file "$SECRETS" "$1")
  printf '%s' "$cur"
}
hex32() {
  if command -v openssl >/dev/null 2>&1; then openssl rand -hex 32
  else head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; fi
}
store() { # $1=name $2=value ; fills the blank line for the name, or appends. The value goes through the environment, not argv.
  tmp="$F.tmp.$$"
  NAME="$1" VALUE="$2" awk '
    BEGIN { name = ENVIRON["NAME"]; value = ENVIRON["VALUE"] }
    index($0, name "=") == 1 { if (!done) { print name "=" value; done = 1 }; next }
    { print }
    END { if (!done) print name "=" value }' "$F" > "$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$F"
}

project="${COMPOSE_PROJECT_NAME:-}"
[ -n "$project" ] || project=$(in_file "$F" COMPOSE_PROJECT_NAME)
[ -n "$project" ] || project=$(basename "$here" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')

if volumes=$(docker volume ls --quiet 2>/dev/null); then docker_ok=1; else docker_ok=0; volumes=""; fi

refused=0
password() { # $1=name $2=volume that fixes the password once it exists, or "-"
  if [ -n "$(current "$1")" ]; then echo "  $1: already-set"; return 0; fi
  if [ "$2" != "-" ]; then
    if [ "$docker_ok" != 1 ]; then
      echo "  $1: NOT generated - Docker could not be asked whether its database already exists. Start Docker and run again." >&2
      refused=1; return 0
    fi
    if printf '%s\n' "$volumes" | grep -qx "${project}_$2"; then
      echo "  $1: NOT generated - the volume ${project}_$2 exists, and its database keeps the password it was created with." >&2
      echo "      Put that password in $F (see 'Upgrading an existing install' in README.md)." >&2
      refused=1; return 0
    fi
  fi
  store "$1" "$(hex32)"
  echo "  $1: generated"
}

password POSTIZ_JWT_SECRET -
password TWENTY_DB_PASSWORD twenty_db
password METABASE_DB_PASSWORD metabase_db
password POSTIZ_DB_PASSWORD postiz_db

echo "  bundle settings: $F"
if [ "$refused" != 0 ]; then
  echo "  some passwords are still missing; the stack will not start until they are set." >&2
  exit 1
fi
