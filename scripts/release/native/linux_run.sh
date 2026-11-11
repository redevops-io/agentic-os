#!/bin/bash -p
# linux_run.sh : the Linux native build of a release commit, in a throwaway Lima VM, driven from this Mac. Nothing is signed, pushed, tagged, published or uploaded; no credential is used;
# the VM has no host mount. It builds the commit twice in different directories (as the c317 run did), then twice more in ONE directory without and with SOURCE_DATE_EPOCH (the
# reproducibility experiment), collects the artifacts of all six builds, and copies them out with a SHA256SUMS. The VM is deleted at the end unless --keep-vm.
#
#   linux_run.sh --repo DIR --ref REF --commit SHA40 --out NEWDIR --mirrors DIR [--vm NAME] [--keep-vm]
#
# --mirrors holds redevops-connectors.git and knowledge-frontier.git (bare) with the tags v0.1.1 and v0.2.1; they are copied into the VM and used instead of GitHub for those two packages only.
set -euo pipefail
PATH=/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO=""; REF=""; COMMIT=""; OUT=""; MIRRORS=""; VM="aos-lnx-rel-$(date -u +%Y%m%d%H%M)"; KEEP=0
while [ $# -gt 0 ]; do case "$1" in --repo) REPO="$2"; shift 2;; --ref) REF="$2"; shift 2;; --commit) COMMIT="$2"; shift 2;; --out) OUT="$2"; shift 2;; --mirrors) MIRRORS="$2"; shift 2;; --vm) VM="$2"; shift 2;; --keep-vm) KEEP=1; shift;; *) echo "unknown $1" >&2; exit 2;; esac; done
[[ "$COMMIT" =~ ^[0-9a-f]{40}$ ]] && [ -d "$REPO" ] && [ -n "$REF" ] && [ -n "$OUT" ] && [ -d "$MIRRORS/redevops-connectors.git" ] && [ -d "$MIRRORS/knowledge-frontier.git" ] || { sed -n 2,12p "$0" >&2; exit 2; }
[ ! -e "$OUT" ] || { echo "REFUSED: $OUT exists" >&2; exit 2; }
[ "$(git -C "$REPO" rev-parse "$REF^{commit}")" = "$COMMIT" ] || { echo "REFUSED: $REF is not $COMMIT" >&2; exit 2; }
[ -z "$(git -C "$REPO" status --porcelain)" ] || { echo "REFUSED: the repository is not clean" >&2; exit 2; }
TREE="$(git -C "$REPO" rev-parse "$COMMIT^{tree}")"
SDE="$(git -C "$REPO" show -s --format=%ct "$COMMIT")"
mkdir -p "$OUT"/{artifacts,evidence,home} && OUT="$(cd "$OUT" && pwd -P)"
# Lima puts a unix socket under LIMA_HOME and the path limit is about 100 characters: the instance home is a SHORT directory (throwaway; the VM is deleted at the end)
LIMA_HOME="/private/tmp/aol-${COMMIT:0:6}"; mkdir -p "$LIMA_HOME"
export LIMA_HOME HOME="$OUT/home"
L() { env -i PATH="$PATH" HOME="$HOME" LIMA_HOME="$LIMA_HOME" LANG=C limactl "$@"; }
S() { L shell --tty=false "$VM" -- bash -lc "$1"; }     # run a command in the VM
log() { echo "[$(date -u +%H:%M:%SZ)] $*" | tee -a "$OUT/evidence/driver.log"; }
cleanup() { if [ "$KEEP" = 0 ]; then L delete -f "$VM" >/dev/null 2>&1 || true; log "VM $VM deleted"; else log "VM $VM kept"; fi; }
trap cleanup EXIT
log "linux_run: commit $COMMIT tree $TREE ref $REF; SOURCE_DATE_EPOCH for the experiment = $SDE (the commit time); VM $VM; LIMA_HOME $LIMA_HOME"
git -C "$REPO" bundle create "$OUT/artifacts/src-${COMMIT:0:8}.bundle" "$REF" >/dev/null 2>&1
git bundle verify "$OUT/artifacts/src-${COMMIT:0:8}.bundle" >/dev/null 2>&1 || { echo "bundle does not verify" >&2; exit 3; }
git -C "$REPO" show "$COMMIT:packaging/toolchain.env" > "$OUT/evidence/toolchain.env"
tar -C "$MIRRORS" -cf "$OUT/artifacts/mirror.tar" redevops-connectors.git knowledge-frontier.git
for t in "redevops-connectors.git v0.1.1" "knowledge-frontier.git v0.2.1"; do set -- $t; git -C "$MIRRORS/$1" rev-parse "refs/tags/$2" >/dev/null || { echo "mirror $1 lacks tag $2" >&2; exit 3; }; done
log "creating the VM"; L create --name="$VM" --tty=false "$HERE/lima-linux.yaml" >>"$OUT/evidence/driver.log" 2>&1
L start "$VM" >>"$OUT/evidence/driver.log" 2>&1; log "VM started"
L copy "$OUT/artifacts/src-${COMMIT:0:8}.bundle" "$VM:src.bundle"; L copy "$OUT/artifacts/mirror.tar" "$VM:mirror.tar"
L copy "$OUT/evidence/toolchain.env" "$VM:/tmp/toolchain.env"
for f in vm_provision.sh vm_build.sh vm_collect.sh vm_compare.sh; do L copy "$HERE/$f" "$VM:$f"; done
S 'mkdir -p ~/mirror && tar -C ~/mirror -xf ~/mirror.tar && ls ~/mirror'
log "provisioning (pins from packaging/toolchain.env)"; S 'bash ~/vm_provision.sh' >"$OUT/evidence/provision.log" 2>&1
S 'uname -a; lsb_release -d; . ~/.cargo/env; rustc --version; cargo --version; cargo tauri --version; ~/.local/bin/uv --version; git --version' > "$OUT/evidence/vm-versions.txt" 2>&1 || true
build() { # LABEL NAME [SDE]
  log "build $1 -> $2 ${3:+(SOURCE_DATE_EPOCH=$3)}"
  S "bash ~/vm_build.sh $1 $COMMIT $TREE $3" >"$OUT/evidence/build-$2.out" 2>&1 && S "bash ~/vm_collect.sh $1 $2" >"$OUT/evidence/collect-$2.out" 2>&1
}
build b1 b1;  build b2 b2           # two directories, no SOURCE_DATE_EPOCH: the c317 comparison
build bx x1;  build bx x2           # ONE directory built twice, no SOURCE_DATE_EPOCH: separates path effects from time effects
build by y1 "$SDE"; build by y2 "$SDE"   # ONE directory built twice with SOURCE_DATE_EPOCH = the commit time
log "comparing the collected builds inside the VM"
for pair in "b1 b2" "x1 x2" "y1 y2"; do set -- $pair; S "bash ~/vm_compare.sh $1 $2" > "$OUT/evidence/compare-$1-$2.out" 2>&1 || true; done
log "copying the artifacts out"
S 'cd ~ && tar -cf art.tar art' && L copy "$VM:art.tar" "$OUT/artifacts/art.tar" && tar -C "$OUT/artifacts" -xf "$OUT/artifacts/art.tar" && rm -f "$OUT/artifacts/art.tar"
( cd "$OUT/artifacts" && find . -type f ! -name SHA256SUMS | LC_ALL=C sort | while IFS= read -r f; do shasum -a 256 "$f"; done > SHA256SUMS )
log "done: $(wc -l < "$OUT/artifacts/SHA256SUMS" | tr -d ' ') files hashed"
