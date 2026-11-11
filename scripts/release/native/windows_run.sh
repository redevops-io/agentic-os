#!/bin/bash -p
# windows_run.sh : the Windows native build of a release commit on a disposable Azure VM, driven from this Mac with the owner's EXISTING authenticated `az` session. Nothing is signed, pushed,
# tagged, published or uploaded outside the throwaway resource group; the group is deleted at the end (unless --keep) and the deletion is checked. Cost: about 2 USD (public retail estimate
# for a Standard_D8s_v4 and a 256 GB disk for a few hours); a larger spend or a credential request stops the run.
#
#   windows_run.sh --repo DIR --ref REF --commit SHA40 --out NEWDIR --mirrors DIR [--location eastus] [--size Standard_D8s_v4] [--keep]
#
# It builds the commit twice in two directories (as the c317 run did), then twice in ONE directory without SOURCE_DATE_EPOCH and twice with it (the reproducibility experiment).
# The VM admin password is generated, held in a shell variable only and never printed or written; no inbound port is opened (run-command goes through the Azure agent).
set -euo pipefail
PATH=/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"; W="$HERE/windows"
REAL_HOME="$HOME"
REPO=""; REF=""; COMMIT=""; OUT=""; MIRRORS=""; LOC="eastus"; SIZE="Standard_D8s_v4"; KEEP=0
while [ $# -gt 0 ]; do case "$1" in --repo) REPO="$2"; shift 2;; --ref) REF="$2"; shift 2;; --commit) COMMIT="$2"; shift 2;; --out) OUT="$2"; shift 2;; --mirrors) MIRRORS="$2"; shift 2;; --location) LOC="$2"; shift 2;; --size) SIZE="$2"; shift 2;; --keep) KEEP=1; shift;; *) echo "unknown $1" >&2; exit 2;; esac; done
[[ "$COMMIT" =~ ^[0-9a-f]{40}$ ]] && [ -d "$REPO" ] && [ -n "$REF" ] && [ -n "$OUT" ] && [ -d "$MIRRORS/redevops-connectors.git" ] && [ -d "$MIRRORS/knowledge-frontier.git" ] || { sed -n 2,12p "$0" >&2; exit 2; }
[ ! -e "$OUT" ] || { echo "REFUSED: $OUT exists" >&2; exit 2; }
[ "$(git -C "$REPO" rev-parse "$REF^{commit}")" = "$COMMIT" ] || { echo "REFUSED: $REF is not $COMMIT" >&2; exit 2; }
[ -z "$(git -C "$REPO" status --porcelain)" ] || { echo "REFUSED: the repository is not clean" >&2; exit 2; }
TREE="$(git -C "$REPO" rev-parse "$COMMIT^{tree}")"; SDE="$(git -C "$REPO" show -s --format=%ct "$COMMIT")"
mkdir -p "$OUT"/artifacts "$OUT"/evidence "$OUT"/tmp; OUT="$(cd "$OUT" && pwd -P)"; chmod 700 "$OUT/tmp"
az() { env -i PATH="$PATH" HOME="$REAL_HOME" LANG=en_US.UTF-8 AZURE_CORE_ONLY_SHOW_ERRORS=1 az "$@"; }
log() { echo "[$(date -u +%H:%M:%SZ)] $*" | tee -a "$OUT/evidence/driver.log"; }
az account show --query "{subscription:name,user:user.name}" -o tsv >/dev/null 2>&1 || { echo "REFUSED: no authenticated Azure CLI session (log in yourself first); this driver never logs in for you" >&2; exit 2; }
TAG="$(date -u +%Y%m%d)-${COMMIT:0:6}"; RG="aos-rel-win-$TAG"; SA="aosrel$(openssl rand -hex 6)"; VM="winbuild"
[ "$(az group exists -n "$RG")" = false ] || { echo "REFUSED: resource group $RG exists" >&2; exit 2; }
[ -n "$(az vm list-skus -l "$LOC" --resource-type virtualMachines --query "[?name=='$SIZE' && length(restrictions)==\`0\`].name" -o tsv)" ] || { echo "REFUSED: VM size $SIZE is restricted or unknown in $LOC for this subscription (pick another with --size)" >&2; exit 2; }
DEL_BY="$(date -u -v+3d +%Y-%m-%d)"
cleanup() {
  if [ "$KEEP" = 0 ]; then
    log "deleting resource group $RG"; az group delete -n "$RG" --yes --no-wait >/dev/null 2>&1 || true
    for i in $(seq 1 60); do
      if [ "$(az group exists -n "$RG" 2>/dev/null)" = false ]; then log "resource group $RG is gone (checked)"; echo "deleted, checked $(date -u +%FT%TZ)" > "$OUT/evidence/rg-deletion-proof.txt"; rm -rf "${OUT:?}/tmp"; return; fi
      sleep 20
    done
    log "WARNING: resource group $RG still exists after 20 minutes: delete it by hand (az group delete -n $RG)"
  else log "resource group $RG kept (--keep)"; fi
  rm -rf "${OUT:?}/tmp"
}
trap cleanup EXIT
log "windows_run: commit $COMMIT tree $TREE ref $REF; SOURCE_DATE_EPOCH for the experiment $SDE; group $RG in $LOC (delete-by $DEL_BY)"
az account show --query "{subscription:name,id:id}" -o json > "$OUT/evidence/az-account.json"
az group list --query "[].name" -o tsv | sort > "$OUT/evidence/groups-before.txt"; az vm list --query "length(@)" -o tsv > "$OUT/evidence/vm-count-before.txt"
git -C "$REPO" bundle create "$OUT/artifacts/src-${COMMIT:0:8}.bundle" "$REF" >/dev/null 2>&1
git bundle verify "$OUT/artifacts/src-${COMMIT:0:8}.bundle" >/dev/null 2>&1 || { echo "bundle does not verify" >&2; exit 3; }
BSHA="$(shasum -a 256 "$OUT/artifacts/src-${COMMIT:0:8}.bundle" | cut -d' ' -f1)"
tar -C "$MIRRORS" -cf "$OUT/tmp/mirror.tar" redevops-connectors.git knowledge-frontier.git
az group create -n "$RG" -l "$LOC" --tags purpose=release-qualification "delete-by=$DEL_BY" -o none; date -u +%FT%TZ > "$OUT/evidence/rg-created-at.txt"
az storage account create -g "$RG" -n "$SA" --sku Standard_LRS --https-only true --min-tls-version TLS1_2 --allow-blob-public-access false -o none
KEY="$(az storage account keys list -g "$RG" -n "$SA" --query '[0].value' -o tsv)"
az storage container create --account-name "$SA" --account-key "$KEY" -n qual -o none
EXP="$(date -u -v+14H +%Y-%m-%dT%H:%MZ)"; echo "$EXP" > "$OUT/evidence/sas-expiry.txt"
SAS="$(az storage container generate-sas --account-name "$SA" --account-key "$KEY" -n qual --permissions racwdl --expiry "$EXP" --https-only -o tsv)"
SASHEX="$(printf '%s' "$SAS" | xxd -p | tr -d '\n')"
up() { az storage blob upload --account-name "$SA" --account-key "$KEY" -c qual -n "$2" -f "$1" --overwrite -o none --no-progress; }
up "$OUT/artifacts/src-${COMMIT:0:8}.bundle" in/src.bundle; up "$OUT/tmp/mirror.tar" in/mirror.tar
for f in lib.ps1 w_job.ps1 w_build.ps1; do up "$W/$f" "scripts/$f"; done
log "creating the VM ($SIZE, Windows Server 2022, 256 GB, no inbound port)"
PW="Aa1!$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-20)"
az vm create -g "$RG" -n "$VM" --image MicrosoftWindowsServer:WindowsServer:2022-datacenter-azure-edition:latest --size "$SIZE" --admin-username azadmin --admin-password "$PW" \
   --os-disk-size-gb 256 --storage-sku Premium_LRS --public-ip-sku Standard --nsg-rule NONE --tags purpose=release-qualification -o none
unset PW
az vm show -g "$RG" -n "$VM" --query "{size:hardwareProfile.vmSize,image:storageProfile.imageReference,disk:storageProfile.osDisk.diskSizeGb}" -o json > "$OUT/evidence/vm.json"
vmrun() { # NAME FILE LIB(0|1) [key=value ...]
  local name="$1" file="$2" lib="$3"; shift 3; local p="$OUT/tmp/$name.ps1"
  # a PowerShell param() block must be the first statement: it is moved in front of the prepended lib.ps1
  python3 - "$W/lib.ps1" "$file" "$lib" > "$p" <<'PY'
import re, sys
libf, f, uselib = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
body = open(f, encoding="utf-8").read().split("\n")
param = [l for l in body if re.match(r"^param\(.*\)\s*$", l)][:1]
body = [l for l in body if not (param and l == param[0])]
out = param + ([open(libf, encoding="utf-8").read()] if uselib else []) + body
sys.stdout.write("\n".join(out))
PY
  local ts; ts="$(date -u +%Y%m%dT%H%M%SZ)"; mkdir -p "$OUT/evidence/runlogs"
  if [ $# -gt 0 ]; then
    az vm run-command invoke -g "$RG" -n "$VM" --command-id RunPowerShellScript --scripts @"$p" --parameters "$@" -o json > "$OUT/evidence/runlogs/$name-$ts.json" 2> "$OUT/evidence/runlogs/$name-$ts.err" || true
  else
    az vm run-command invoke -g "$RG" -n "$VM" --command-id RunPowerShellScript --scripts @"$p" -o json > "$OUT/evidence/runlogs/$name-$ts.json" 2> "$OUT/evidence/runlogs/$name-$ts.err" || true
  fi
  python3 - "$OUT/evidence/runlogs/$name-$ts.json" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    print("\n".join(v.get("message", "") for v in d.get("value", [])))
except Exception as e:
    print("ERR parsing", e)
PY
}
BASE="https://$SA.blob.core.windows.net/qual"
log "bootstrap"; vmrun bootstrap "$W/w_bootstrap.ps1" 1 "Base=$BASE" "SasHex=$SASHEX" "Names=lib.ps1,w_job.ps1,w_build.ps1" > "$OUT/evidence/bootstrap.txt" 2>&1
log "toolchain (git, python, 7-zip, uv, VS Build Tools, rust, tauri-cli)"; vmrun toolchain "$W/w_toolchain.ps1" 1 > "$OUT/evidence/toolchain.log" 2>&1
build() { # LABEL ROOT [SDE]
  log "build $1 in $2 ${3:+(SOURCE_DATE_EPOCH=$3)}"
  local js; js="{\"Label\":\"$1\",\"Root\":\"$(printf '%s' "$2" | sed 's/\\/\\\\/g')\",\"Commit\":\"$COMMIT\",\"Tree\":\"$TREE\",\"BundleSha\":\"$BSHA\",\"Sde\":\"${3:-}\"}"
  vmrun "build-$1" "$W/w_task.ps1" 1 "Name=b-$1" "Script=w_build.ps1" "ArgB64=$(printf '%s' "$js" | base64 | tr -d '\n')" "MaxMinutes=80" > "$OUT/evidence/build-$1.log" 2>&1
  tail -2 "$OUT/evidence/build-$1.log" | tee -a "$OUT/evidence/driver.log"
}
build f1 'C:\r1\src'; build f2 'C:\r2\src'
build fx1 'C:\rx\src'; build fx2 'C:\rx\src'
build fy1 'C:\ry\src' "$SDE"; build fy2 'C:\ry\src' "$SDE"
log "fetching the artifacts"
az storage blob download-batch --account-name "$SA" --account-key "$KEY" -s qual --pattern 'out/*' -d "$OUT/artifacts/vmout" -o none --no-progress 2>>"$OUT/evidence/driver.log" || true
( cd "$OUT/artifacts" && find . -type f ! -name SHA256SUMS | LC_ALL=C sort | while IFS= read -r f; do shasum -a 256 "$f"; done > SHA256SUMS )
az group show -n "$RG" --query "{id:id,tags:tags}" -o json > "$OUT/evidence/rg.json"; az resource list -g "$RG" --query "[].{n:name,t:type}" -o tsv > "$OUT/evidence/rg-resources.txt"
unset KEY SAS SASHEX
log "done: $(wc -l < "$OUT/artifacts/SHA256SUMS" | tr -d ' ') files hashed"
