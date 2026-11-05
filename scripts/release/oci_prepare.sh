#!/bin/bash
# oci_prepare.sh - prepare the release OCI images LOCALLY and prove them. NOTHING is pushed anywhere
# except to a throwaway registry:2 container bound to 127.0.0.1.
#
# What it does (stages, in order; select with OCI_STAGES="infra build ..."):
#   infra    throwaway registry (aos-oci-registry, 127.0.0.1 only) + docker-container buildx builder
#   lint     Dockerfile policy (digest-pinned base, USER, HEALTHCHECK, no curl|sh)
#   build    linux/amd64 + linux/arm64, --provenance=mode=max --sbom=true, pushed to the local registry
#   rebuild  clean rebuild (--no-cache) and comparison of per-arch and index digests
#   sbom     BuildKit SBOM vs a separate syft SBOM (CycloneDX + SPDX), package-count comparison
#   scan     grype + trivy against the per-arch digests (counts by severity, HIGH/CRITICAL detail)
#   layers   history/config/layer secret scan of every per-arch image
#   sign     THROWAWAY cosign key pair: sign by digest, attest SBOM + provenance
#   verify   the consumer flow (cosign verify / verify-attestation / imagetools inspect, digest-pinned
#            compose + k8s examples) and the negative tests (tampered digest, wrong key, unsigned)
#   run      start every image (both architectures) with synthetic config, loopback ports, hardened flags
#   export   save the registry content as a tarball (so the digests can be re-served after approval)
#   cleanup  remove exactly the aos-rel-* / aos-oci-registry resources and delete the throwaway keys
#
# Safe by construction: re-executes itself under `env -i` with an allowlist (the caller's environment,
# including any credentials, never reaches docker/cosign/syft/grype/trivy); uses an EMPTY docker config
# (no registry credentials, no credential helper); never signs with a real key; never touches
# containers that are not named aos-rel-* / aos-oci-registry.
#
# Usage:   OCI_OUT=/path/to/out scripts/release/oci_prepare.sh
# Needs:   docker (+ buildx, compose), cosign, syft, grype, trivy, python3, openssl, curl, git.
#          syft/grype/trivy: OCI_TOOLS_BIN=<dir holding them> if they are not on PATH.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
OUT=${OCI_OUT:-$PWD/oci-prepare-out}
mkdir -p "$OUT"
OUT=$(cd "$OUT" && pwd)

# ---------------------------------------------------------------- clean environment re-exec
if [ -z "${OCI_CLEAN_ENV:-}" ]; then
  mkdir -p "$OUT/home" "$OUT/tmp" "$OUT/docker-config/cli-plugins" "$OUT/logs"
  [ -f "$OUT/docker-config/config.json" ] || echo '{}' > "$OUT/docker-config/config.json"
  docker_host=${DOCKER_HOST:-$(docker context inspect --format '{{.Endpoints.docker.Host}}' 2>/dev/null || true)}
  [ -n "$docker_host" ] || { echo "no docker endpoint found" >&2; exit 2; }
  for plugin in docker-buildx docker-compose; do
    for d in "$HOME/.docker/cli-plugins" /usr/local/lib/docker/cli-plugins /Applications/Docker.app/Contents/Resources/cli-plugins /usr/libexec/docker/cli-plugins /usr/lib/docker/cli-plugins; do
      if [ -e "$d/$plugin" ]; then ln -sf "$(cd "$d" && pwd -P)/$plugin" "$OUT/docker-config/cli-plugins/$plugin"; break; fi
    done
  done
  tools=${OCI_TOOLS_BIN:-}
  exec env -i PATH="${tools:+$tools:}/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin" HOME="$OUT/home" TMPDIR="$OUT/tmp" \
    LANG=en_US.UTF-8 DOCKER_HOST="$docker_host" DOCKER_CONFIG="$OUT/docker-config" DOCKER_BUILDKIT=1 \
    BUILDX_NO_DEFAULT_ATTESTATIONS=1 OCI_CLEAN_ENV=1 OCI_OUT="$OUT" OCI_REGISTRY_PORT="${OCI_REGISTRY_PORT:-55100}" \
    OCI_STAGES="${OCI_STAGES:-}" OCI_TAG="${OCI_TAG:-}" OCI_SOURCE_DATE_EPOCH="${OCI_SOURCE_DATE_EPOCH:-}" \
    OCI_KEEP="${OCI_KEEP:-}" "$0" "$@"
fi

# ---------------------------------------------------------------- configuration
PORT=${OCI_REGISTRY_PORT:-55100}
REG=localhost:$PORT
REGISTRY_NAME=aos-oci-registry
BUILDER=aos-rel-builder
VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' "$REPO/pyproject.toml" | head -1)
SHORT=$(git -C "$REPO" rev-parse --short=7 HEAD)
TAG=${OCI_TAG:-$VERSION-$SHORT}
SDE=${OCI_SOURCE_DATE_EPOCH:-$(git -C "$REPO" log -1 --format=%ct)}
PLATFORMS=linux/amd64,linux/arm64
# name:Dockerfile:container port:local test port
IMAGES=("redevops-runtime:Dockerfile:8080:55301" "redevops-projects:Dockerfile.projects:8787:55302")
TOOLS=$REPO/scripts/release/oci_tools.py
W=$OUT/work; L=$OUT/logs; R=$OUT/results; K=$OUT/keys-throwaway
mkdir -p "$W" "$L" "$R" "$OUT/scans" "$OUT/sbom" "$OUT/attest" "$OUT/consumer" "$OUT/cache/grype" "$OUT/cache/trivy"
export GRYPE_DB_CACHE_DIR=$OUT/cache/grype GRYPE_CHECK_FOR_APP_UPDATE=false SYFT_CHECK_FOR_APP_UPDATE=false
export TRIVY_CACHE_DIR=$OUT/cache/trivy

say() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$L/oci_prepare.log" >&2; }
t0() { T0=$(date +%s); }
t1() { printf '%s\t%s\n' "$1" "$(( $(date +%s) - T0 ))" >> "$R/timings.tsv"; }
img_names() { for e in "${IMAGES[@]}"; do echo "${e%%:*}"; done; }
field() { local e; for e in "${IMAGES[@]}"; do if [ "${e%%:*}" = "$1" ]; then IFS=: read -r _ f cp lp <<< "$e"; case $2 in file) echo "$f";; cport) echo "$cp";; lport) echo "$lp";; esac; fi; done; }
jq_py() { python3 -I -c "import json,sys; d=json.load(open(sys.argv[1])); print($2)" "$1"; }   # jq_py file 'expr over d'
arches() { echo amd64 arm64; }
digest_of() { jq_py "$R/digests-$1-$2.json" "d['images']['linux/$3'] if '$3' != 'index' else d['index']"; }   # name stage arch|index

# ---------------------------------------------------------------- stages
stage_infra() {
  say "infra: registry $REGISTRY_NAME on 127.0.0.1:$PORT, builder $BUILDER"
  docker version --format 'docker client {{.Client.Version}} server {{.Server.Version}} {{.Server.Os}}/{{.Server.Arch}}' | tee "$R/versions.txt"
  docker buildx version >> "$R/versions.txt"; cosign version 2>&1 | sed -n 's/^GitVersion: */cosign /p' >> "$R/versions.txt"
  syft version | sed -n 's/^Version: */syft /p' >> "$R/versions.txt"; grype version | sed -n 's/^Version: */grype /p' >> "$R/versions.txt"
  trivy --version | head -1 | sed 's/^Version/trivy/' >> "$R/versions.txt"
  git -C "$REPO" rev-parse HEAD | sed 's/^/source commit /' >> "$R/versions.txt"
  if [ -n "$(git -C "$REPO" status --porcelain)" ]; then say "WARNING: working tree is not clean; provenance will not match a commit"; echo "tree: DIRTY" >> "$R/versions.txt"; else echo "tree: clean" >> "$R/versions.txt"; fi
  if [ -z "$(docker ps -aq --filter "name=^${REGISTRY_NAME}\$")" ]; then
    docker pull -q registry:2 > /dev/null
    docker image inspect registry:2 --format 'registry image {{index .RepoDigests 0}}' >> "$R/versions.txt"
    docker run -d --name "$REGISTRY_NAME" -p "127.0.0.1:$PORT:5000" --label aos-rel=1 registry:2 > /dev/null
  fi
  for i in $(seq 1 30); do curl -fsS -o /dev/null "http://127.0.0.1:$PORT/v2/" && break; sleep 1; done
  cat > "$W/buildkitd.toml" <<EOF
[registry."$REG"]
  http = true
  insecure = true
EOF
  if ! docker buildx inspect "$BUILDER" > /dev/null 2>&1; then
    docker buildx create --name "$BUILDER" --driver docker-container --driver-opt network=host --config "$W/buildkitd.toml" --bootstrap > "$L/builder-create.log" 2>&1
  fi
  docker buildx inspect "$BUILDER" | sed -n 's/^Platforms: */builder platforms: /p;s/^BuildKit version: */buildkit /p' >> "$R/versions.txt"
}

stage_lint() {
  say "lint: Dockerfile policy"
  : > "$R/lint.jsonl"
  for n in $(img_names); do python3 -I "$TOOLS" dockerfile-lint "$REPO/$(field "$n" file)" | python3 -I -c 'import json,sys; print(json.dumps(json.load(sys.stdin)))' >> "$R/lint.jsonl"; done
  cat "$R/lint.jsonl"
  if grep -q '"all_bases_digest_pinned": false\|"has_user": false\|"has_healthcheck": false\|"curl_pipe_or_latest_download": true\|"copy_dot": true' "$R/lint.jsonl"; then say "lint FAILED"; return 1; fi
}

build_one() {   # name tag nocache(0|1)
  local n=$1 tag=$2 nc=$3 f; f=$(field "$n" file)
  t0
  docker buildx build --builder "$BUILDER" $([ "$nc" = 1 ] && echo --no-cache) --platform "$PLATFORMS" -f "$REPO/$f" \
    --build-arg SOURCE_DATE_EPOCH="$SDE" \
    --output "type=image,name=$REG/$n:$tag,push=true,rewrite-timestamp=true" \
    --provenance=mode=max --sbom=true --metadata-file "$W/meta-$n-$tag.json" "$REPO" > "$L/build-$n-$tag.log" 2>&1
  t1 "build $n $tag nocache=$nc"
  python3 -I "$TOOLS" digests "$REG/$n" "$tag" > "$R/digests-$n-$([ "$nc" = 1 ] && echo rebuild || echo build).json"
}

stage_build() {
  say "build: $PLATFORMS tag=$TAG SOURCE_DATE_EPOCH=$SDE"
  for n in $(img_names); do build_one "$n" "$TAG" 0; python3 -I -c "import json;d=json.load(open('$R/digests-$n-build.json'));print('$n index',d['index']);[print('  ',k,v) for k,v in d['images'].items()]"; done
}

stage_rebuild() {
  say "rebuild: --no-cache, same inputs"
  : > "$R/repro.txt"
  for n in $(img_names); do
    build_one "$n" "$TAG-rebuild" 1
    {
      echo "== $n"
      for a in amd64 arm64; do
        d1=$(digest_of "$n" build "$a"); d2=$(digest_of "$n" rebuild "$a")
        echo "linux/$a image digest: build=$d1 rebuild=$d2 $([ "$d1" = "$d2" ] && echo IDENTICAL || echo DIFFERENT)"
      done
      i1=$(digest_of "$n" build index); i2=$(digest_of "$n" rebuild index)
      echo "index digest: build=$i1 rebuild=$i2 $([ "$i1" = "$i2" ] && echo IDENTICAL || echo DIFFERENT)"
    } | tee -a "$R/repro.txt"
    for a in amd64 arm64; do
      d1=$(digest_of "$n" build "$a"); d2=$(digest_of "$n" rebuild "$a")
      if [ "$d1" != "$d2" ]; then
        docker pull -q --platform "linux/$a" "$REG/$n@$d1" > /dev/null; docker pull -q --platform "linux/$a" "$REG/$n@$d2" > /dev/null
        docker save "$REG/$n@$d1" -o "$W/a.tar"; docker save "$REG/$n@$d2" -o "$W/b.tar"
        { echo "layer comparison $n linux/$a:"; python3 -I "$TOOLS" layerdiff "$W/a.tar" "$W/b.tar"; } | tee -a "$R/repro.txt"
        rm -f "$W/a.tar" "$W/b.tar"
      fi
    done
    # why the index digest differs: the attestation manifests (provenance carries build ids/timestamps)
    for a in amd64 arm64; do
      for tg in "$TAG" "$TAG-rebuild"; do
        docker buildx imagetools inspect "$REG/$n:$tg" --format '{{json .Provenance}}' > "$W/prov-$n-$tg.json"
        docker buildx imagetools inspect "$REG/$n:$tg" --format '{{json .SBOM}}' > "$W/bksbom-$n-$tg.json"
      done
      python3 -I "$TOOLS" jsondiff "$W/prov-$n-$TAG.json" "$W/prov-$n-$TAG-rebuild.json" "linux/$a" | sed "s/^/provenance linux\/$a differs at: /" | tee -a "$R/repro.txt"
      python3 -I "$TOOLS" jsondiff "$W/bksbom-$n-$TAG.json" "$W/bksbom-$n-$TAG-rebuild.json" "linux/$a" | sed "s/^/buildkit-sbom linux\/$a differs at: /" | tee -a "$R/repro.txt"
    done
  done
}

stage_sbom() {
  say "sbom: BuildKit SBOM vs syft (separate tool)"
  : > "$R/sbom-compare.jsonl"
  for n in $(img_names); do
    idx=$(digest_of "$n" build index)
    docker buildx imagetools inspect "$REG/$n@$idx" --format '{{json .SBOM}}' > "$OUT/sbom/$n.buildkit-spdx.json"
    for a in $(arches); do
      ad=$(digest_of "$n" build "$a")
      syft "registry:$REG/$n@$ad" -q -o "cyclonedx-json=$OUT/sbom/$n-$a.syft.cdx.json" -o "spdx-json=$OUT/sbom/$n-$a.syft.spdx.json"
      python3 -I "$TOOLS" sbomcmp "$OUT/sbom/$n.buildkit-spdx.json" "$OUT/sbom/$n-$a.syft.cdx.json" "linux/$a" | python3 -I -c "import json,sys; d=json.load(sys.stdin); d['image']='$n'; print(json.dumps(d))" >> "$R/sbom-compare.jsonl"
    done
  done
  python3 -I -c "
import json
for l in open('$R/sbom-compare.jsonl'):
    d=json.loads(l)
    print(d['image'], d['arch'], 'buildkit(excl files)=%d syft(excl files)=%d equal_sets=%s only_bk=%d only_syft=%d' % (d['buildkit_packages_excl_files'], d['syft_cyclonedx_packages_excl_files'], d['equal_name_version_sets'], len(d['only_in_buildkit']), len(d['only_in_syft'])))"
}

stage_scan() {
  say "scan: grype + trivy (DB updates are public downloads)"
  grype db update > "$L/grype-db-update.log" 2>&1 || true
  grype db status > "$R/grype-db-status.txt" 2>&1 || true
  : > "$R/scan-summary.jsonl"
  for n in $(img_names); do for a in $(arches); do
    ad=$(digest_of "$n" build "$a")
    grype "registry:$REG/$n@$ad" -q -o json > "$OUT/scans/$n-$a.grype.json" 2> "$L/grype-$n-$a.err"
    trivy image --insecure --skip-version-check --scanners vuln,secret --format json --output "$OUT/scans/$n-$a.trivy.json" "$REG/$n@$ad" > "$L/trivy-$n-$a.log" 2>&1
    python3 -I "$TOOLS" vulnsummary "$OUT/scans/$n-$a.grype.json" "$OUT/scans/$n-$a.trivy.json" > "$OUT/scans/$n-$a.summary.json"
    python3 -I -c "
import json
d=json.load(open('$OUT/scans/$n-$a.summary.json')); print(json.dumps({'image':'$n','arch':'$a','digest':'$ad','grype':d['grype'],'trivy':d['trivy']}))" >> "$R/scan-summary.jsonl"
  done; done
  cat "$R/scan-summary.jsonl"
}

stage_layers() {
  say "layers: history, config and layer secret scan"
  : > "$R/secretscan-summary.jsonl"
  for n in $(img_names); do for a in $(arches); do
    ad=$(digest_of "$n" build "$a")
    docker pull -q --platform "linux/$a" "$REG/$n@$ad" > /dev/null
    docker history --no-trunc --format '{{.CreatedBy}}' "$REG/$n@$ad" > "$OUT/scans/$n-$a.history.txt"
    docker save "$REG/$n@$ad" -o "$W/$n-$a.tar"
    python3 -I "$TOOLS" secretscan "$W/$n-$a.tar" > "$OUT/scans/$n-$a.secretscan.json"
    python3 -I -c "
import json
d=json.load(open('$OUT/scans/$n-$a.secretscan.json'))
print(json.dumps({'image':'$n','arch':'$a','layers':d['layers'],'user':d['config_user'],'healthcheck':bool(d['healthcheck']),'env_names':d['config_env_names'],
 'suspicious_file_names':d['suspicious_file_names'],'pattern_hits':d['pattern_hits'],'history_secret_like':d['history_created_by_secret_like']}))" >> "$R/secretscan-summary.jsonl"
    rm -f "$W/$n-$a.tar"
  done; done
  cat "$R/secretscan-summary.jsonl" | python3 -I -c "
import json,sys
for l in sys.stdin:
    d=json.loads(l); print(d['image'],d['arch'],'user=%s healthcheck=%s name_hits=%d pattern_hits=%d history_secret_like=%d' % (d['user'],d['healthcheck'],len(d['suspicious_file_names']),len(d['pattern_hits']),len(d['history_secret_like'])))"
}

cosign_env() { COSIGN_PASSWORD=$(cat "$K/password") "$@"; }
SIGN_FLAGS=(--signing-config "$K/empty-signing-config.json" --allow-http-registry --yes)
VERIFY_FLAGS=(--insecure-ignore-tlog --allow-http-registry)

stage_sign() {
  say "sign: throwaway key pair, sign by digest, attest SBOM + provenance"
  mkdir -p "$K"; chmod 700 "$K"
  if [ ! -f "$K/cosign.key" ]; then
    openssl rand -hex 16 > "$K/password"
    ( cd "$K" && COSIGN_PASSWORD=$(cat password) cosign generate-key-pair > /dev/null 2>&1 )
    ( cd "$K" && mkdir -p wrong && cd wrong && COSIGN_PASSWORD=$(cat ../password) cosign generate-key-pair > /dev/null 2>&1 )
    cosign signing-config create --out "$K/empty-signing-config.json" > /dev/null 2>&1
  fi
  shasum -a 256 "$K/cosign.pub" | awk '{print "throwaway public key sha256 " $1}' >> "$R/versions.txt"
  for n in $(img_names); do
    idx=$(digest_of "$n" build index)
    cosign_env cosign sign --key "$K/cosign.key" "${SIGN_FLAGS[@]}" --recursive -a "release-qualification=local-throwaway" "$REG/$n@$idx" > "$L/sign-$n.log" 2>&1
    docker buildx imagetools inspect "$REG/$n@$idx" --format '{{json .Provenance}}' > "$W/prov-$n.json"
    for a in $(arches); do
      ad=$(digest_of "$n" build "$a")
      python3 -I -c "import json; print(json.dumps(json.load(open('$W/prov-$n.json'))['linux/$a']['SLSA']))" > "$OUT/attest/$n-$a.slsa-provenance-predicate.json"
      cosign_env cosign attest --key "$K/cosign.key" "${SIGN_FLAGS[@]}" --type cyclonedx --predicate "$OUT/sbom/$n-$a.syft.cdx.json" "$REG/$n@$ad" > "$L/attest-sbom-$n-$a.log" 2>&1
      cosign_env cosign attest --key "$K/cosign.key" "${SIGN_FLAGS[@]}" --type slsaprovenance1 --predicate "$OUT/attest/$n-$a.slsa-provenance-predicate.json" "$REG/$n@$ad" > "$L/attest-prov-$n-$a.log" 2>&1
    done
  done
  say "signed + attested"
}

expect_fail() {   # label command...   (a verification that MUST fail)
  local label=$1; shift
  if "$@" > "$L/neg-$(echo "$label" | tr ' /' '__').log" 2>&1; then echo "NEGATIVE-TEST-FAILED(verification passed): $label" | tee -a "$R/verify.txt"; NEG_BAD=1
  else echo "ok (refused as required): $label" | tee -a "$R/verify.txt"; fi
}
expect_pass() {
  local label=$1; shift
  if "$@" > "$L/pos-$(echo "$label" | tr ' /' '__').log" 2>&1; then echo "ok (verified): $label" | tee -a "$R/verify.txt"
  else echo "POSITIVE-TEST-FAILED: $label" | tee -a "$R/verify.txt"; POS_BAD=1; fi
}

stage_verify() {
  say "verify: consumer flow + negative tests"
  : > "$R/verify.txt"; NEG_BAD=0; POS_BAD=0
  local pub=$K/cosign.pub wrong=$K/wrong/cosign.pub
  for n in $(img_names); do
    idx=$(digest_of "$n" build index)
    expect_pass "verify index $n" cosign verify --key "$pub" "${VERIFY_FLAGS[@]}" "$REG/$n@$idx"
    docker buildx imagetools inspect "$REG/$n@$idx" > "$OUT/consumer/imagetools-$n.txt"
    for a in $(arches); do
      ad=$(digest_of "$n" build "$a")
      expect_pass "verify per-arch $n/$a" cosign verify --key "$pub" "${VERIFY_FLAGS[@]}" "$REG/$n@$ad"
      expect_pass "verify-attestation cyclonedx $n/$a" cosign verify-attestation --key "$pub" --type cyclonedx "${VERIFY_FLAGS[@]}" "$REG/$n@$ad"
      expect_pass "verify-attestation slsaprovenance1 $n/$a" cosign verify-attestation --key "$pub" --type slsaprovenance1 "${VERIFY_FLAGS[@]}" "$REG/$n@$ad"
      # the attested subject must be the digest the consumer pinned
      cosign verify-attestation --key "$pub" --type slsaprovenance1 "${VERIFY_FLAGS[@]}" "$REG/$n@$ad" 2>/dev/null | python3 -I -c "
import base64,json,sys
ok=True
for line in sys.stdin:
    env=json.loads(line); st=json.loads(base64.b64decode(env['payload']))
    ok = ok and st['subject'][0]['digest']['sha256']=='${ad#sha256:}' and st['predicateType']=='https://slsa.dev/provenance/v1'
print('provenance subject == pinned digest and predicateType slsa v1:', ok); sys.exit(0 if ok else 1)" > "$L/subject-$n-$a.log" 2>&1 \
        && echo "ok (verified): attested subject matches pinned digest $n/$a" | tee -a "$R/verify.txt" || { echo "POSITIVE-TEST-FAILED: subject match $n/$a" | tee -a "$R/verify.txt"; POS_BAD=1; }
    done
    # ---- negatives
    local bad=${idx%?}$([ "${idx: -1}" = 0 ] && echo 1 || echo 0)
    expect_fail "tampered digest (last hex char changed) $n" cosign verify --key "$pub" "${VERIFY_FLAGS[@]}" "$REG/$n@$bad"
    expect_fail "wrong key $n" cosign verify --key "$wrong" "${VERIFY_FLAGS[@]}" "$REG/$n@$idx"
    expect_fail "wrong key attestation $n/arm64" cosign verify-attestation --key "$wrong" --type cyclonedx "${VERIFY_FLAGS[@]}" "$REG/$n@$(digest_of "$n" build arm64)"
    ridx=$(digest_of "$n" rebuild index)
    if [ "$ridx" != "$idx" ]; then expect_fail "unsigned index (the clean rebuild's own index) $n" cosign verify --key "$pub" "${VERIFY_FLAGS[@]}" "$REG/$n@$ridx"; fi
    # a different image pushed under a tag the consumer might follow: unsigned, must be refused
    mkdir -p "$W/neg-ctx"; printf 'FROM %s\nRUN echo tampered > /tampered\n' "$(sed -n 's/^FROM //p' "$REPO/$(field "$n" file)" | head -1)" > "$W/neg-ctx/Dockerfile"
    docker buildx build --builder "$BUILDER" --platform linux/arm64 --provenance=false --sbom=false --output "type=image,name=$REG/$n:tampered,push=true" "$W/neg-ctx" > "$L/neg-build-$n.log" 2>&1
    expect_fail "unsigned tampered image pulled by tag $n:tampered" cosign verify --key "$pub" "${VERIFY_FLAGS[@]}" "$REG/$n:tampered"
    expect_fail "no attestation on the tampered image $n:tampered" cosign verify-attestation --key "$pub" --type cyclonedx "${VERIFY_FLAGS[@]}" "$REG/$n:tampered"
    # an attestation type that was never made
    expect_fail "attestation type never attached (spdxjson) $n/arm64" cosign verify-attestation --key "$pub" --type spdxjson "${VERIFY_FLAGS[@]}" "$REG/$n@$(digest_of "$n" build arm64)"
  done
  # digest-pinned consumer examples
  local ridx pidx; ridx=$(digest_of redevops-runtime build index); pidx=$(digest_of redevops-projects build index)
  cat > "$OUT/consumer/compose.yaml" <<EOF
# Consumer example: images are referenced by the VERIFIED index digest, never by a tag.
# Verify first:  cosign verify --key cosign.pub $REG/redevops-runtime@$ridx
services:
  runtime:
    image: $REG/redevops-runtime@$ridx
    ports: ["127.0.0.1:55303:8080"]
    environment: ["AGENTIC_OS_HOME=/data"]
    read_only: true
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    # writable paths of the control plane: /data (permissions key + grants) and the Fleet workdir
    tmpfs: ["/tmp", "/app/.agentic-os:uid=10001,gid=10001"]
    volumes: ["data:/data"]
  projects:
    image: $REG/redevops-projects@$pidx
    ports: ["127.0.0.1:55304:8787"]
    environment: ["PROJECTS_ALLOWED_HOSTS=127.0.0.1:55304"]
    read_only: true
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    tmpfs: ["/tmp", "/home/app:uid=10001,gid=10001"]
volumes:
  data: {}
EOF
  cat > "$OUT/consumer/k8s.yaml" <<EOF
# Consumer example (not applied: no cluster is used here). The image is pinned by digest; an admission
# policy (Kyverno/Sigstore policy-controller) would verify the cosign signature against the release key.
apiVersion: apps/v1
kind: Deployment
metadata: {name: redevops-runtime}
spec:
  replicas: 1
  selector: {matchLabels: {app: redevops-runtime}}
  template:
    metadata: {labels: {app: redevops-runtime}}
    spec:
      securityContext: {runAsNonRoot: true, runAsUser: 10001, runAsGroup: 10001, fsGroup: 10001, seccompProfile: {type: RuntimeDefault}}
      containers:
        - name: runtime
          image: $REG/redevops-runtime@$ridx
          env: [{name: AGENTIC_OS_HOME, value: /data}]
          ports: [{name: http, containerPort: 8080}]
          securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {drop: [ALL]}}
          readinessProbe: {httpGet: {path: /health, port: http}, initialDelaySeconds: 5}
          livenessProbe: {httpGet: {path: /health, port: http}, initialDelaySeconds: 10}
          volumeMounts: [{name: data, mountPath: /data}, {name: tmp, mountPath: /tmp}, {name: fleet, mountPath: /app/.agentic-os}]
      volumes: [{name: data, emptyDir: {}}, {name: tmp, emptyDir: {}}, {name: fleet, emptyDir: {}}]
EOF
  echo "note: k8s.yaml is an example only: not applied and not schema-validated (no cluster)" | tee -a "$R/verify.txt"
  # run the digest-pinned compose example as a consumer would (verification above already passed)
  docker compose -p aos-rel-consumer -f "$OUT/consumer/compose.yaml" up -d --pull always > "$L/compose-up.log" 2>&1 || { echo "POSITIVE-TEST-FAILED: compose up" | tee -a "$R/verify.txt"; POS_BAD=1; }
  local ok=0
  for i in $(seq 1 100); do
    st=$(docker inspect --format '{{.State.Health.Status}}' aos-rel-consumer-runtime-1 aos-rel-consumer-projects-1 2>/dev/null | sort -u | tr '\n' ' ')
    [ "$st" = "healthy " ] && { ok=1; break; }
    [ "$(docker ps -q --filter name=^aos-rel-consumer- | wc -l | tr -d ' ')" = 2 ] || break
    sleep 3
  done
  [ "$ok" = 1 ] && echo "ok: digest-pinned consumer compose reaches (healthy) with read_only + cap_drop ALL + no-new-privileges" | tee -a "$R/verify.txt" || { echo "POSITIVE-TEST-FAILED: consumer compose not healthy ($st)" | tee -a "$R/verify.txt"; docker compose -p aos-rel-consumer -f "$OUT/consumer/compose.yaml" logs > "$L/compose-logs.log" 2>&1 || true; POS_BAD=1; }
  curl -fsS "http://127.0.0.1:55303/health" | tee -a "$R/verify.txt"; echo >> "$R/verify.txt"
  docker compose -p aos-rel-consumer -f "$OUT/consumer/compose.yaml" down -v > "$L/compose-down.log" 2>&1 || true
  say "verify: positives_failed=$POS_BAD negatives_that_wrongly_passed=$NEG_BAD"
  [ "$POS_BAD" = 0 ] && [ "$NEG_BAD" = 0 ]
}

stage_run() {
  say "run: start every image on both architectures (synthetic config, loopback only)"
  : > "$R/run-summary.jsonl"
  for n in $(img_names); do for a in $(arches); do for mode in hardened; do
    ad=$(digest_of "$n" build "$a"); cp=$(field "$n" cport); lp=$(field "$n" lport); cn=aos-rel-run-$n-$a-$mode
    [ "$a" = arm64 ] && hp=$lp || hp=$((lp + 100))
    docker rm -f "$cn" > /dev/null 2>&1 || true
    extra=(--read-only --cap-drop ALL --security-opt no-new-privileges:true --tmpfs /tmp)
    if [ "$n" = redevops-runtime ]; then extra+=(-e AGENTIC_OS_HOME=/data --tmpfs /data:uid=10001,gid=10001 --tmpfs /app/.agentic-os:uid=10001,gid=10001); else extra+=(-e "PROJECTS_ALLOWED_HOSTS=127.0.0.1:$hp" --tmpfs /home/app:uid=10001,gid=10001); fi
    t0
    docker run -d --name "$cn" --platform "linux/$a" -p "127.0.0.1:$hp:$cp" --label aos-rel=1 "${extra[@]}" "$REG/$n@$ad" > /dev/null
    st=starting
    for i in $(seq 1 80); do st=$(docker inspect --format '{{.State.Health.Status}}' "$cn"); [ "$st" = healthy ] || [ "$st" = unhealthy ] && break; [ "$(docker inspect --format '{{.State.Running}}' "$cn")" = true ] || break; sleep 3; done
    t1 "run $n $a to-$st"
    uid=$(docker exec "$cn" id -u 2>/dev/null || echo "?")
    machine=$(docker exec "$cn" python -c 'import platform; print(platform.machine())' 2>/dev/null || echo "?")
    if [ "$n" = redevops-runtime ]; then probe=$(curl -fsS "http://127.0.0.1:$hp/health" || echo FAIL)
    else probe=$(python3 -I "$HERE/oci_tools.py" probe-projects "$cn" "$hp" 2>&1 || echo FAIL); fi
    docker inspect "$cn" --format '{{json .State.Health}}' > "$OUT/scans/$cn.health.json"
    docker logs "$cn" > "$L/run-$cn.log" 2>&1 || true
    python3 -I -c "
import json
print(json.dumps({'image':'$n','arch':'$a','mode':'$mode','digest':'$ad','health':'$st','uid':'$uid','machine':'$machine','probe':'''$probe'''.replace('\n',' ')[:300]}))" >> "$R/run-summary.jsonl"
    docker rm -f "$cn" > /dev/null
  done; done; done
  cat "$R/run-summary.jsonl"
}

stage_export() {
  say "export: registry content tarball (re-servable after owner approval; digests preserved)"
  docker cp "$REGISTRY_NAME:/var/lib/registry/." - | gzip -n > "$OUT/registry-data.tar.gz"
  shasum -a 256 "$OUT/registry-data.tar.gz" | tee "$R/registry-data.sha256"
  {
    echo "# digests (local registry $REG; nothing is on ghcr.io)"
    for n in $(img_names); do echo "$n tag=$TAG index=$(digest_of "$n" build index)"; for a in $(arches); do echo "  linux/$a $(digest_of "$n" build "$a")"; done; done
  } > "$R/DIGESTS.txt"
  cat "$R/DIGESTS.txt"
}

stage_cleanup() {
  say "cleanup: aos-rel-* / $REGISTRY_NAME only"
  docker compose -p aos-rel-consumer -f "$OUT/consumer/compose.yaml" down -v > /dev/null 2>&1 || true
  for c in $(docker ps -aq --filter "name=^aos-rel-run-" ); do docker rm -f "$c" > /dev/null 2>&1 || true; done
  docker rm -f -v "$REGISTRY_NAME" > /dev/null 2>&1 || true
  docker buildx rm --force "$BUILDER" > /dev/null 2>&1 || true
  for id in $(docker image ls --format '{{.ID}} {{.Repository}}' | awk -v p="^$REG/" '$2 ~ p {print $1}' | sort -u); do docker rmi -f "$id" > /dev/null 2>&1 || true; done
  if [ -z "${OCI_KEEP:-}" ] && [ -d "$K" ]; then
    : > "$K/cosign.key"; rm -f "$K/cosign.key" "$K/wrong/cosign.key" "$K/password" "$K/wrong/cosign.pub"
    echo "throwaway private keys and password deleted; public key kept: $K/cosign.pub" | tee -a "$R/versions.txt"
  fi
  docker ps -a --format '{{.Names}}' | grep -E '^aos-rel-|^aos-oci-registry$' || echo "no aos-rel-* / registry containers left"
  docker buildx ls | grep -c aos-rel-builder || true
}

ALL="infra lint build rebuild sbom scan layers sign verify run export cleanup"
for s in ${OCI_STAGES:-$ALL}; do
  say "=== stage $s"
  "stage_$s"
done
say "done; results in $OUT/results (summary files: DIGESTS.txt repro.txt sbom-compare.jsonl scan-summary.jsonl secretscan-summary.jsonl verify.txt run-summary.jsonl)"
