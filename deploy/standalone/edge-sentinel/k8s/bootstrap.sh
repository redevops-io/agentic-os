#!/bin/sh
# One-time bootstrap for the Kubernetes/k3s deployment. Idempotent: an existing Secret is never replaced.
#   usage: ./bootstrap.sh [-y]      (run from this directory; uses the CURRENT kubectl context, which it prints first)
# It creates the namespace and a Secret with random values (piped to kubectl, never written to disk or printed), applies the
# manifests, waits for CrowdSec and registers the watcher login Sentinel uses. Read the operator password afterwards with the
# command it prints.
set -eu
cd "$(dirname "$0")"
NS=edge-sentinel
echo "kubectl context: $(kubectl config current-context) (server: $(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}'))"
if [ "${1:-}" != "-y" ]; then
  printf 'Apply Edge Sentinel to this cluster? [y/N] '; read -r a; [ "$a" = y ] || [ "$a" = Y ] || { echo aborted; exit 1; }
fi
gen() { head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; }
kubectl apply -f namespace.yaml
if ! kubectl -n "$NS" get secret edge-sentinel >/dev/null 2>&1; then
  OP="$(gen)"; BK="$(gen)"; MP="$(gen)"
  kubectl -n "$NS" apply -f - >/dev/null <<YAML
apiVersion: v1
kind: Secret
metadata: {name: edge-sentinel, namespace: $NS}
type: Opaque
stringData: {operator-password: "$OP", crowdsec-bouncer-key: "$BK", crowdsec-machine-password: "$MP"}
YAML
  echo "created Secret edge-sentinel"
fi
# EDGE_SENTINEL_IMAGE=<registry/name@sha256:digest or :tag> installs that image instead of the one in kustomization.yaml
# (verify-k3s.sh uses it to test a published digest); the manifests in this directory are not edited.
if [ -n "${EDGE_SENTINEL_IMAGE:-}" ]; then
  kubectl kustomize . | sed "s|image: ghcr.io/redevops-io/edge-sentinel:[^[:space:]]*|image: ${EDGE_SENTINEL_IMAGE}|" | kubectl apply -f -
else
  kubectl apply -k .
fi
kubectl -n "$NS" rollout status deploy/crowdsec --timeout=300s
MACHINE_PW="$(kubectl -n "$NS" get secret edge-sentinel -o jsonpath='{.data.crowdsec-machine-password}' | base64 -d)"
# cscli echoes the credentials it registers on stderr; neither stream may reach a terminal or a CI log
kubectl -n "$NS" exec deploy/crowdsec -- cscli machines add edge-sentinel --password "$MACHINE_PW" -f - --force >/dev/null 2>&1 \
  || { echo "registering the watcher login failed (kubectl -n $NS logs deploy/crowdsec)" >&2; exit 1; }
kubectl -n "$NS" rollout status deploy/edge-sentinel --timeout=300s
echo
echo "Edge Sentinel is running in namespace $NS. Reach it with:   kubectl -n $NS port-forward svc/edge-sentinel 8203:80"
echo "Sign in as 'operator'; the password is:                      kubectl -n $NS get secret edge-sentinel -o jsonpath='{.data.operator-password}' | base64 -d"
