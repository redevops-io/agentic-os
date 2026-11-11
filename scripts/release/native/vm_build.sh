#!/bin/bash
# vm_build.sh LABEL TIP TREE [SOURCE_DATE_EPOCH] : runs INSIDE the build VM. A clean clone from ~/src.bundle into ~/LABEL/src, checked out at TIP (the release commit; the tree must equal TREE),
# then packaging/build-all.sh under an env -i allowlist. Two sibling packages (redevops-connectors, knowledge-frontier) are resolved from the local mirrors in ~/mirror because their licence
# tags are not on GitHub yet: the ONLY URL rewriting, done through git's environment configuration, recorded in the build log header. Everything else resolves against the real hosts.
set -euo pipefail
LABEL="$1"; TIP="$2"; TREE="$3"; SDE="${4:-}"
[[ "$TIP" =~ ^[0-9a-f]{40}$ && "$TREE" =~ ^[0-9a-f]{40}$ ]] || { echo "TIP and TREE must be 40-hex"; exit 9; }
B="$HOME/$LABEL"; rm -rf "$B"; mkdir -p "$B"
git clone -q "$HOME/src.bundle" "$B/src"
cd "$B/src"; git checkout -q --detach "$TIP"
[ "$(git rev-parse HEAD)" = "$TIP" ] && [ "$(git rev-parse 'HEAD^{tree}')" = "$TREE" ] || { echo "SOURCE MISMATCH"; exit 9; }
[ -z "$(git status --porcelain)" ] || { echo "DIRTY"; exit 9; }
mkdir -p "$B/home" "$B/tmp"
M="file://$HOME/mirror"
E=(PATH="$HOME/.cargo/bin:$HOME/.local/bin:/usr/sbin:/usr/bin:/sbin:/bin" HOME="$B/home" TMPDIR="$B/tmp" LANG=C.UTF-8 TERM=dumb
   CARGO_HOME="$HOME/.cargo" RUSTUP_HOME="$HOME/.rustup" UV_PYTHON_PREFERENCE=managed UV_NO_MODIFY_PATH=1
   GIT_CONFIG_COUNT=3 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0="$B/src"
   GIT_CONFIG_KEY_1="url.$M/redevops-connectors.insteadOf" GIT_CONFIG_VALUE_1=https://github.com/redevops-io/redevops-connectors
   GIT_CONFIG_KEY_2="url.$M/knowledge-frontier.insteadOf" GIT_CONFIG_VALUE_2=https://github.com/redevops-io/knowledge-frontier)
[ -z "$SDE" ] || E+=(SOURCE_DATE_EPOCH="$SDE")
{
  echo "build $LABEL start $(date -u +%FT%TZ) in $B/src; tip $TIP tree $TREE; SOURCE_DATE_EPOCH=${SDE:-unset}"
  echo "url rewriting (only these two): https://github.com/redevops-io/redevops-connectors and .../knowledge-frontier -> $M/ (tags v0.1.1 and v0.2.1 are not on GitHub yet)"
} | tee "$HOME/build-$LABEL.log"
set +e
env -i "${E[@]}" ./packaging/build-all.sh 2>&1 | tee -a "$HOME/build-$LABEL.log"
rc=${PIPESTATUS[0]}
echo "build $LABEL end $(date -u +%FT%TZ) rc=$rc" | tee -a "$HOME/build-$LABEL.log"
exit "$rc"
