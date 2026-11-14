#!/bin/sh
# Verifies the Kubernetes install on a REAL k3s: starts a throwaway k3s (the rancher/k3s image, privileged, in Docker), installs
# Edge Sentinel with the documented k8s/bootstrap.sh, runs the route walk and smoke.sh (alert -> case -> approve -> execute ->
# read-back -> pod restart -> state kept) against it, prints the pod security context, and removes everything it created.
#
#   usage: ./verify-k3s.sh [IMAGE]      IMAGE defaults to the digest of the current release (see docs/standalone/edge-sentinel.md);
#                                       a local image works too: ./verify-k3s.sh my/image:tag (it is loaded into k3s)
#
# Safety: it only ever talks to the k3s it started (its own kubeconfig file; your KUBECONFIG and contexts are never read or
# changed), under unique container/network/volume names, and it never touches any other cluster. Needs docker, kubectl, python3, curl.
set -eu
cd "$(dirname "$0")"
K3S_IMAGE="${K3S_IMAGE:-rancher/k3s:v1.36.6-k3s1@sha256:88d3fac10eb4a429d3c5275f4868fe4b13620f6bbca5bdb5dca0627ac76ae2a0}"
IMAGE="${1:-${EDGE_SENTINEL_IMAGE:-ghcr.io/redevops-io/edge-sentinel@sha256:90fd0ab8be167535c63ef57c124f17f5ee436f255e47c9106187c49b6cb1e31b}}"
SUFFIX="$(head -c 4 /dev/urandom | od -An -tx1 | tr -d ' \n')"
NAME="sentinel-k3s-$SUFFIX"
API_PORT="${K3S_API_PORT:-$((20000 + $(od -An -N2 -tu2 /dev/urandom | tr -d ' ') % 10000))}"
FWD_PORT="${K3S_FORWARD_PORT:-$((30000 + $(od -An -N2 -tu2 /dev/urandom | tr -d ' ') % 10000))}"
WORK="$(mktemp -d)"
export KUBECONFIG="$WORK/kubeconfig"
PF_PID=""
step() { echo; echo "== $*"; }
cleanup() {
  [ -z "$PF_PID" ] || { pkill -P "$PF_PID" 2>/dev/null || true; kill "$PF_PID" 2>/dev/null || true; wait "$PF_PID" 2>/dev/null || true; }
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker volume rm "$NAME-data" >/dev/null 2>&1 || true
  rm -f "$WORK/kubeconfig" "$WORK/pf.log" 2>/dev/null; rmdir "$WORK" 2>/dev/null || true
  echo "removed container $NAME"
}
if [ -n "${K3S_KEEP:-}" ]; then trap 'echo "kept: docker rm -f $NAME; kubeconfig: $KUBECONFIG"' EXIT; else trap cleanup EXIT; fi

step "starting k3s ($K3S_IMAGE) as $NAME on 127.0.0.1:$API_PORT"
docker run -d --name "$NAME" --privileged --tmpfs /run --tmpfs /var/run -v "$NAME-data":/var/lib/rancher/k3s \
  -p "127.0.0.1:$API_PORT:6443" "$K3S_IMAGE" server --disable=traefik --disable=metrics-server --tls-san 127.0.0.1 >/dev/null
i=0; until docker exec "$NAME" test -s /etc/rancher/k3s/k3s.yaml 2>/dev/null; do i=$((i + 1)); [ "$i" -gt 60 ] && { echo "k3s did not start" >&2; docker logs "$NAME" 2>&1 | tail -20; exit 1; }; sleep 2; done
docker exec "$NAME" cat /etc/rancher/k3s/k3s.yaml | sed "s#https://127.0.0.1:6443#https://127.0.0.1:$API_PORT#" > "$WORK/kubeconfig"
chmod 600 "$WORK/kubeconfig"
i=0; until kubectl get nodes 2>/dev/null | grep -q " Ready"; do i=$((i + 1)); [ "$i" -gt 60 ] && { echo "k3s node not ready" >&2; exit 1; }; sleep 3; done
kubectl version 2>/dev/null | sed -n 's/^Server Version: /k3s server: /p'
kubectl get nodes -o wide | tail -1 | cut -c1-120

case "$IMAGE" in
  ghcr.io/*) echo "image $IMAGE is pulled by k3s itself (anonymous)";;
  *) step "loading local image $IMAGE into k3s"; docker save "$IMAGE" | docker exec -i "$NAME" ctr -n k8s.io images import - >/dev/null;;
esac

step "installing with k8s/bootstrap.sh"
EDGE_SENTINEL_IMAGE="$IMAGE" ./k8s/bootstrap.sh -y 2>&1 | grep -vE "^$" | tail -12
kubectl -n edge-sentinel get pods -o wide | cut -c1-140
echo "pod security context:"; kubectl -n edge-sentinel get deploy edge-sentinel -o jsonpath='{.spec.template.spec.containers[0].securityContext}{"\n"}{.spec.template.spec.securityContext}{"\n"}'
echo "running image: $(kubectl -n edge-sentinel get pod -l app.kubernetes.io/name=edge-sentinel -o jsonpath='{.items[0].status.containerStatuses[0].imageID}')"
echo "network policies: $(kubectl -n edge-sentinel get networkpolicy -o name | tr '\n' ' ')"

step "enabling real execution for the test (throwaway cluster only): SENTINEL_BLOCK_ENABLED=1, test range 203.0.113.0/24 bannable"
kubectl -n edge-sentinel set env deploy/edge-sentinel SENTINEL_BLOCK_ENABLED=1 SENTINEL_BLOCK_ALLOW_NETWORKS=203.0.113.0/24 >/dev/null
kubectl -n edge-sentinel rollout status deploy/edge-sentinel --timeout=180s | tail -1

step "port-forward and checks"
# a forward that restarts itself: a pod restart (part of smoke.sh) ends a plain `kubectl port-forward`
( while :; do kubectl -n edge-sentinel port-forward svc/edge-sentinel "$FWD_PORT:80" >"$WORK/pf.log" 2>&1 || true; sleep 1; done ) &
PF_PID=$!
i=0; until curl -sf "http://127.0.0.1:$FWD_PORT/readyz" >/dev/null 2>&1; do i=$((i + 1)); [ "$i" -gt 30 ] && { echo "port-forward/readyz failed" >&2; cat "$WORK/pf.log" >&2; exit 1; }; sleep 2; done
PW="$(kubectl -n edge-sentinel get secret edge-sentinel -o jsonpath='{.data.operator-password}' | base64 -d)"
python3 ./routewalk.py "http://127.0.0.1:$FWD_PORT" operator "$PW" | tail -4
SMOKE_BASE="http://127.0.0.1:$FWD_PORT" SMOKE_USER=operator SMOKE_PASSWORD="$PW" \
SMOKE_CROWDSEC="kubectl -n edge-sentinel exec deploy/crowdsec --" \
SMOKE_RESTART="kubectl -n edge-sentinel rollout restart deploy/edge-sentinel && kubectl -n edge-sentinel rollout status deploy/edge-sentinel --timeout=180s" \
  ./smoke.sh
echo "VERIFY-K3S OK"
