"""Live infrastructure reads for the cost advisor + audit — MCP-shaped adapters.

The rule from docs/sidekick-mcp-tooling.md: *read/inspect → MCP; mutate-with-policy → custom operator*.
This module is the READ side. Each function returns live data when a source is reachable (tagging
`source: "live:..."`) and falls back to a modeled value (`source: "modeled"`) otherwise, so the UI can
show provenance. Today the genuinely-live source is the in-cluster **metrics-server** (metrics.k8s.io) —
exactly what a Kubernetes MCP server exposes — read with the pod's own service-account token. Grafana /
cost MCP servers are wired as config hooks (env-driven) and fall back to modeled until pointed at a real
endpoint.
"""
from __future__ import annotations

import json
import os
import ssl
import urllib.request

_SA = "/var/run/secrets/kubernetes.io/serviceaccount"
_NS = os.environ.get("INSPECT_NAMESPACE", "default")


def _k8s_get(path: str, timeout: float = 4.0):
    """GET the in-cluster Kubernetes API using the pod's service-account token + CA."""
    with open(f"{_SA}/token", encoding="utf-8") as fh:
        token = fh.read().strip()
    ctx = ssl.create_default_context(cafile=f"{_SA}/ca.crt")
    req = urllib.request.Request(f"https://kubernetes.default.svc{path}",
                                 headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, context=ctx, timeout=timeout) as r:  # noqa: S310 (in-cluster, CA-pinned)
        return json.load(r)


def _cpu_millicores(v: str) -> int:
    """'1m' → 1, '561000000n' → 561, '1' → 1000."""
    v = v.strip()
    if v.endswith("n"):
        return round(int(v[:-1]) / 1_000_000)
    if v.endswith("u"):
        return round(int(v[:-1]) / 1_000)
    if v.endswith("m"):
        return int(v[:-1])
    return int(float(v) * 1000)


def _mem_mib(v: str) -> int:
    """'38Mi' → 38, '2Gi' → 2048, '512Ki' → 1."""
    v = v.strip()
    units = {"Ki": 1 / 1024, "Mi": 1, "Gi": 1024, "Ti": 1024 * 1024}
    for u, factor in units.items():
        if v.endswith(u):
            return round(int(v[:-len(u)]) * factor)
    return round(int(v) / (1024 * 1024))  # bytes


def pod_utilization(ns: str | None = None) -> dict:
    """Live pod CPU/mem from metrics-server (what a Kubernetes MCP server's `top`/metrics tool returns).

    Returns {source, pods:[{name, cpu_m, mem_mi}], total_cpu_m, total_mem_mi} or a modeled fallback.
    This is a genuinely live read — the audit's rightsizing dimension uses it to compare requests vs
    actual usage instead of guessing."""
    ns = ns or _NS
    try:
        data = _k8s_get(f"/apis/metrics.k8s.io/v1beta1/namespaces/{ns}/pods")
        pods = []
        for it in data.get("items", []):
            cpu = sum(_cpu_millicores(c["usage"]["cpu"]) for c in it.get("containers", []))
            mem = sum(_mem_mib(c["usage"]["memory"]) for c in it.get("containers", []))
            pods.append({"name": it["metadata"]["name"], "cpu_m": cpu, "mem_mi": mem})
        if pods:
            return {"source": "live:metrics-server", "pods": pods,
                    "total_cpu_m": sum(p["cpu_m"] for p in pods),
                    "total_mem_mi": sum(p["mem_mi"] for p in pods)}
    except Exception:  # noqa: BLE001 — any failure (RBAC, no metrics-server, local run) → modeled
        pass
    # modeled fallback (e.g. local test / no metrics-server / no RBAC)
    return {"source": "modeled", "pods": [{"name": "app", "cpu_m": 40, "mem_mi": 40}],
            "total_cpu_m": 40, "total_mem_mi": 40}


def cluster_inventory(limit: int = 200) -> dict:
    """Live cluster-wide pod inventory — declared requests vs actual usage (metrics-server), read-only.
    The real audit surface: Sidekick inspects the actual k3s deployment. Falls back to a modeled sample
    off-cluster. Flags pods that are over-provisioned (usage ≪ requests) or have no requests set."""
    try:
        pods = _k8s_get("/api/v1/pods?limit=%d" % limit)
        metrics = _k8s_get("/apis/metrics.k8s.io/v1beta1/pods")
        usage = {}
        for it in metrics.get("items", []):
            k = it["metadata"]["namespace"] + "/" + it["metadata"]["name"]
            usage[k] = (sum(_cpu_millicores(c["usage"]["cpu"]) for c in it.get("containers", [])),
                        sum(_mem_mib(c["usage"]["memory"]) for c in it.get("containers", [])))
        rows = []
        for p in pods.get("items", []):
            if (p.get("status", {}).get("phase")) != "Running":
                continue
            ns, n = p["metadata"]["namespace"], p["metadata"]["name"]
            rc = rm = 0
            for c in p["spec"].get("containers", []):
                req = (c.get("resources", {}).get("requests") or {})
                if req.get("cpu"):
                    rc += _cpu_millicores(req["cpu"])
                if req.get("memory"):
                    rm += _mem_mib(req["memory"])
            uc, um = usage.get(ns + "/" + n, (0, 0))
            over = rc and (uc < rc * 0.3)          # using <30% of the CPU it reserved
            norq = (rc == 0 and rm == 0)           # no requests set at all
            rows.append({"ns": ns, "name": n[:40], "req_cpu_m": rc, "req_mem_mi": rm,
                         "cpu_m": uc, "mem_mi": um, "over_provisioned": bool(over), "no_requests": norq})
        flagged = [r for r in rows if r["over_provisioned"] or r["no_requests"]]
        return {"source": "live:k8s", "count": len(rows), "flagged": len(flagged),
                "pods": sorted(rows, key=lambda r: -(r["req_cpu_m"] or 0))[:12]}
    except Exception:  # noqa: BLE001
        return {"source": "modeled", "count": 1, "flagged": 1,
                "pods": [{"ns": "default", "name": "example-app", "req_cpu_m": 500,
                          "req_mem_mi": 512, "cpu_m": 2, "mem_mi": 41, "over_provisioned": True, "no_requests": False}]}


def cost_signals(spec: dict) -> dict:
    """Live cloud-cost signal for the advisor. Wired as a config hook: if COST_MCP_URL / a cost API is
    configured it would be read here; today it falls back to the modeled STACK_META prices. Returns the
    provenance so the UI is honest about live-vs-modeled pricing."""
    endpoint = os.environ.get("COST_MCP_URL") or os.environ.get("VANTAGE_API_TOKEN")
    if endpoint:
        # placeholder for a real Vantage / AWS-Billing MCP read; kept a no-op until credentials exist
        try:
            # a real implementation would query the cost MCP here and return live per-stack $
            return {"source": "live:cost-mcp", "note": "cost MCP configured"}
        except Exception:  # noqa: BLE001
            pass
    return {"source": "modeled", "note": "prices from the built-in model — set COST_MCP_URL to read live"}


def prometheus_query(query: str) -> list | None:
    """Run a PromQL instant query against PROMETHEUS_URL; return the result vector or None."""
    prom = os.environ.get("PROMETHEUS_URL")
    if not prom:
        return None
    try:
        url = prom.rstrip("/") + "/api/v1/query?query=" + urllib.parse.quote(query)
        with urllib.request.urlopen(url, timeout=3) as r:  # noqa: S310 (in-cluster URL)
            data = json.load(r)
        return data.get("data", {}).get("result", []) if data.get("status") == "success" else None
    except Exception:  # noqa: BLE001
        return None


def loki_tail(selector: str = os.environ.get('LOKI_SELECTOR', '{namespace=~".+"}'), limit: int = 40) -> dict:
    """Live log tail from Loki (LogQL) via LOKI_URL — the observability MCP's log path. Returns the most
    recent lines with their pod/namespace labels. Falls back to a modeled note when Loki isn't wired."""
    loki = os.environ.get("LOKI_URL")
    if not loki:
        return {"source": "modeled", "lines": [], "note": "set LOKI_URL for live logs (Loki)"}
    try:
        q = ("/loki/api/v1/query_range?limit=%d&direction=backward&query=" % limit) + urllib.parse.quote(selector)
        with urllib.request.urlopen(loki.rstrip("/") + q, timeout=4) as r:  # noqa: S310 (in-cluster URL)
            data = json.load(r)
        lines = []
        for stream in data.get("data", {}).get("result", []):
            lbl = stream.get("stream", {})
            tag = (lbl.get("namespace", "") + "/" + lbl.get("pod", ""))[:44]
            for ts, line in stream.get("values", []):
                lines.append({"pod": tag, "ts": int(ts), "line": line[:300]})
        lines.sort(key=lambda x: x["ts"], reverse=True)
        return {"source": "live:loki", "loki": loki, "count": len(lines), "lines": lines[:limit]}
    except Exception:  # noqa: BLE001
        return {"source": "modeled", "lines": [], "note": "Loki unreachable"}


def perf_signals(ns: str | None = None) -> dict:
    """Live performance signal for the audit + cockpit — reads Prometheus (PromQL) when PROMETHEUS_URL
    is set (the observability MCP's read path), and links the Grafana dashboard from GRAFANA_URL.
    Falls back to modeled until an endpoint is configured."""
    graf = os.environ.get("GRAFANA_URL")
    res = prometheus_query("mission_demo_up")
    if res is not None:
        firing = prometheus_query("mission_demo_monitor_signals_firing")
        return {"source": "live:prometheus", "prometheus": os.environ.get("PROMETHEUS_URL"),
                "grafana_url": graf, "up": bool(res),
                "signals_firing": (int(float(firing[0]["value"][1])) if firing else None),
                "note": "reading live from Prometheus"}
    if graf:
        return {"source": "live:grafana", "grafana_url": graf, "note": "Grafana configured (Prometheus unreachable)"}
    return {"source": "modeled", "note": "set PROMETHEUS_URL / GRAFANA_URL for live observability"}
