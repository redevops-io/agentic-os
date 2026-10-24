# Egress boundary

Token checks decide *who may do what through our APIs*. They cannot stop a process that opens its own socket, runs `curl`, or
ignores a proxy setting. The network can. This directory is the network half of the authority work.

| File | What it is |
|---|---|
| `proxy.py` | allowlist forward proxy (HTTPS `CONNECT` and plain HTTP), deny by default, SSRF guard, JSON decision log. Standard library only |
| `forward.py` | inbound forwarder: publishes one port of a service that lives on an internal network and relays it to one fixed target |
| `allowlist.txt` | the destinations the proxy will reach. Everything else is refused |
| `Dockerfile` | one small image holding both files |
| `selftest.sh` | live proof on a throwaway network: direct sockets blocked, allowlist enforced, metadata address refused, decisions logged |
| `../apps/compose.egress.yml` | optional overlay that puts the apps stack on an internal network behind the proxy |
| `../k8s/networkpolicy-egress.yaml` | the Kubernetes equivalent (default-deny egress, the proxy as the only exit) |

## Use it (Compose)

```bash
# 1. list what your agents really call (model host, Apollo, Gmail, GitHub...) in deploy/egress/allowlist.txt
python deploy/egress/proxy.py --allowlist deploy/egress/allowlist.txt --check
# 2. start the stack with the overlay (Docker Compose >= 2.24)
docker compose -f deploy/apps/compose.yml -f deploy/apps/compose.egress.yml up -d
# 3. watch decisions; fix a missing destination by editing the allowlist and sending SIGHUP
docker compose logs -f egress
docker compose kill -s HUP egress
# 4. verify the mechanism itself on a throwaway network
deploy/egress/selftest.sh
```

Production preflight: `python -m agentic_os.mission.profile --require-production --require-egress` fails unless `HTTPS_PROXY` is set.

## What the proxy guarantees

* Deny by default. A rule is `host[:ports] [private]`; `*.suffix` matches subdomains only, never the apex or a lookalike.
* A destination is resolved once, every resolved address must be public, and the connection goes to the address that was checked
  (no second lookup, so DNS rebinding cannot swap it). Private, loopback, link-local (including the cloud metadata address), CGNAT,
  multicast and reserved addresses are refused unless the rule is explicitly marked `private`. A literal IP needs an exact rule.
* One JSON line per decision: time, client, method, host, port, allow, matching rule, reason, bytes, duration, and the caller's
  `X-Egress-Identity` header if it sends one. That is the evidence an operator or Edge Sentinel needs.

## What it does not do (read this before relying on it)

* **Opt-in.** The default `compose.yml` is unchanged so existing installs keep working. Official production deployments should use the overlay or the Kubernetes policy.
* **The allowlist is global to the proxy.** Every service on the internal network can reach every listed destination. Per-workload lists need one proxy per workload.
* **No payload inspection.** It decides on destination, not content. An allowed destination can still receive anything a service sends it.
* **`private` rules are real holes.** The default `host.docker.internal:8000 private` lets every service on the network reach a model server on the host. Remove it if the model is hosted elsewhere.
* **Caddy keeps outbound access** (it needs it for certificate issuance) and the three forwarders sit on both networks, but each forwarder relays one fixed port and speaks no protocol.
* **Kubernetes DNS.** Pods must reach `kube-dns`, and a resolver that forwards external names is a DNS-tunnelling channel. On the Docker version tested here (29.9.1) an internal network does not resolve external names, so that channel is closed there; it is open in a cluster unless DNS is also restricted.
* **Not run against a cluster.** The Kubernetes manifest parses as YAML and has not been applied or dry-run against an API server. The image reference in it is a deliberate placeholder: build, publish, sign and pin `deploy/egress` first (it is not published yet).
* **A proxy-unaware client simply cannot connect.** That is the point, but expect to add destinations on first use.
