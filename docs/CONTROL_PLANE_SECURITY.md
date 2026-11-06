# Control plane: credentials and network exposure

`agentic_os.control_plane` (the service in the `redevops-runtime` image and the root and `deploy/` compose files) does not run open:
the mutating routes and the mission routes answer 503 until a credential is configured and 401 without it. The variable names
(`AGENTIC_OS_CONTROL_TOKEN`, the legacy `AGENTIC_OS_API_KEY`, the optional read-only `AGENTIC_OS_DASHBOARD_TOKEN`, tenant and approver
tokens) and the full route table are those of the main line; see `docs/SECURITY_MODEL.md` and `docs/PERMISSIONS_MIGRATION.md`.
Supply the credential at run time (compose `environment`, a Kubernetes Secret), never in the image.

## Network exposure (compose)

The compose files publish the control plane on **loopback only** by default: `"${AOS_BIND_ADDR:-127.0.0.1}:8080:8080"`
(root `docker-compose.yml`, `deploy/docker-compose.yml`, and the Traefik port 80 in the latter). `docker run` examples use
`-p 127.0.0.1:<port>:<port>` for the same reason. To reach the service from other machines, set the address explicitly:

```bash
AOS_BIND_ADDR=0.0.0.0 docker compose up -d     # WARNING: reachable from every network the host is on
```

Warning: `0.0.0.0` exposes the control plane to every network the host is attached to. Before doing it, set
`AGENTIC_OS_CONTROL_TOKEN`, put the service behind a TLS terminating reverse proxy you control, and restrict the source addresses
at the host firewall. Routes that are open by design (`/health`, the HTML shells, `/modules`, `/status`, `/m/<name>/api/*`,
`/docs`, `/openapi.json`) stay readable by anyone who can reach the port. A specific interface address (for example the host's
private address) is better than `0.0.0.0`.
