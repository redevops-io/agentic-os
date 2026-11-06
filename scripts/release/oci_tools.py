#!/usr/bin/env python3
"""Helpers for scripts/release/oci_prepare.sh (standard library only; run with `python3 -I`).

Sub-commands: digests, layerdiff, sbomcmp, vulnsummary, secretscan, jsondiff, probe-projects, dockerfile-lint, layer-scan, config-check.
Every sub-command prints JSON (or text for layerdiff) and never prints secret values: the secret scan
reports file names, line numbers and pattern names only.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import subprocess
import sys
import tarfile
from collections import Counter

SEV_ORDER = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "NEGLIGIBLE", "UNKNOWN"]


def _run(cmd: list[str]) -> bytes:
    return subprocess.run(cmd, check=True, capture_output=True).stdout


def cmd_digests(repo: str, tag: str) -> dict:
    """Index digest (sha256 of the raw index bytes) and per-platform image / attestation digests."""
    raw = _run(["docker", "buildx", "imagetools", "inspect", f"{repo}:{tag}", "--raw"])
    idx = json.loads(raw)
    out = {"ref": f"{repo}:{tag}", "index": "sha256:" + hashlib.sha256(raw).hexdigest(),
           "mediaType": idx.get("mediaType"), "images": {}, "attestations": {}}
    by_digest = {}
    for m in idx["manifests"]:
        pl = m.get("platform", {})
        ann = m.get("annotations", {})
        if ann.get("vnd.docker.reference.type") == "attestation-manifest":
            by_digest[ann["vnd.docker.reference.digest"]] = m["digest"]
        else:
            out["images"][f"{pl.get('os')}/{pl.get('architecture')}"] = m["digest"]
    out["attestations"] = {a: by_digest.get(d) for a, d in out["images"].items() if d in by_digest} | {}
    out["attestation_manifest_of"] = by_digest
    return out


def _load_save(path: str):
    t = tarfile.open(path)
    idx = json.load(t.extractfile("index.json"))

    def blob(d: str) -> bytes:
        return t.extractfile("blobs/sha256/" + d.split(":")[1]).read()

    m = json.loads(blob(idx["manifests"][0]["digest"]))
    if "manifests" in m:
        m = json.loads(blob(m["manifests"][0]["digest"]))
    cfg = json.loads(blob(m["config"]["digest"]))
    return t, m, cfg, blob


def _layer_entries(blob: bytes):
    lt = tarfile.open(fileobj=io.BytesIO(blob))
    return lt, lt.getmembers()


def cmd_layerdiff(a: str, b: str) -> str:
    _, ma, ca, blob_a = _load_save(a)
    _, mb, cb, blob_b = _load_save(b)
    lines = [f"created: {ca.get('created')} vs {cb.get('created')}"]
    strip = lambda h: [{k: v for k, v in x.items() if k != "created"} for x in h]
    lines.append(f"history equal ignoring created: {strip(ca['history']) == strip(cb['history'])}")
    for i, (la, lb) in enumerate(zip(ma["layers"], mb["layers"])):
        if la["digest"] == lb["digest"]:
            lines.append(f"layer {i}: identical")
            continue
        ea, eb = {}, {}
        for store, blobf, layer in ((ea, blob_a, la), (eb, blob_b, lb)):
            lt, members = _layer_entries(blobf(layer["digest"]))
            for mem in members:
                h = hashlib.sha256(lt.extractfile(mem).read()).hexdigest()[:12] if mem.isfile() else "-"
                store[mem.name] = (h, mem.mtime, mem.mode, mem.uid, mem.gid)
        names = set(ea) | set(eb)
        content = sorted(n for n in names if n in ea and n in eb and ea[n][0] != eb[n][0])
        meta = sorted(n for n in names if n in ea and n in eb and ea[n][0] == eb[n][0] and ea[n] != eb[n])
        only = sorted(n for n in names if (n in ea) != (n in eb))
        lines.append(f"layer {i}: DIFFERENT files={len(ea)}/{len(eb)} content_differs={len(content)} "
                     f"metadata_only_differs={len(meta)} only_in_one={len(only)}")
        for n in content[:5]:
            lines.append(f"    content: {n}")
        for n in meta[:3]:
            lines.append(f"    metadata: {n} {ea[n][1:]} vs {eb[n][1:]}")
    return "\n".join(lines)


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def cmd_sbomcmp(bk_path: str, cdx_path: str, arch: str) -> dict:
    """Compare the BuildKit SPDX SBOM (imagetools .SBOM, per platform) with syft's CycloneDX."""
    bk = json.load(open(bk_path))[arch]["SPDX"]
    cdx = json.load(open(cdx_path))
    bk_pk = {}
    for p in bk["packages"]:
        if p.get("primaryPackagePurpose") == "FILE" or p.get("name", "").startswith("/"):
            continue
        if p.get("name", "").startswith("sha256:") or "SPDXRef-DocumentRoot" in p.get("SPDXID", ""):
            continue
        bk_pk[(_norm(p["name"]), p.get("versionInfo", ""))] = p
    sy_pk = {}
    for c in cdx["components"]:
        if c["type"] in ("file", "container"):
            continue
        sy_pk[(_norm(c["name"]), c.get("version", ""))] = c
    # BuildKit's scanner and ours are different syft versions; compare on name only as well.
    bk_names = {k[0] for k in bk_pk}
    sy_names = {k[0] for k in sy_pk}
    return {
        "arch": arch,
        "buildkit_spdx_creators": bk["creationInfo"]["creators"],
        "buildkit_spdx_packages_total": len(bk["packages"]),
        "buildkit_packages_excl_files": len(bk_pk),
        "syft_cyclonedx_components_total": len(cdx["components"]),
        "syft_cyclonedx_packages_excl_files": len(sy_pk),
        "syft_component_types": dict(Counter(c["type"] for c in cdx["components"])),
        "equal_name_version_sets": set(bk_pk) == set(sy_pk),
        "only_in_buildkit": sorted(f"{n}@{v}" for n, v in set(bk_pk) - set(sy_pk))[:50],
        "only_in_syft": sorted(f"{n}@{v}" for n, v in set(sy_pk) - set(bk_pk))[:50],
        "names_only_in_buildkit": sorted(bk_names - sy_names)[:50],
        "names_only_in_syft": sorted(sy_names - bk_names)[:50],
    }


def _fix_text(v) -> str:
    return ",".join(v) if v else "none"


def cmd_vulnsummary(grype_path: str, trivy_path: str) -> dict:
    g = json.load(open(grype_path))
    t = json.load(open(trivy_path))
    g_counts, g_hc = Counter(), {}
    for m in g["matches"]:
        v, a = m["vulnerability"], m["artifact"]
        sev = v["severity"].upper()
        g_counts[sev] += 1
        if sev in ("HIGH", "CRITICAL"):
            key = (a["name"], a["version"], a["type"], v["id"])
            g_hc[key] = {"package": a["name"], "installed": a["version"], "type": a["type"], "id": v["id"],
                         "severity": sev, "fixed_in": _fix_text(v.get("fix", {}).get("versions")),
                         "fix_state": v.get("fix", {}).get("state")}
    t_counts, t_hc = Counter(), {}
    for r in t.get("Results", []):
        for v in r.get("Vulnerabilities") or []:
            sev = v["Severity"].upper()
            t_counts[sev] += 1
            if sev in ("HIGH", "CRITICAL"):
                t_hc[(v["PkgName"], v["InstalledVersion"], r.get("Type"), v["VulnerabilityID"])] = {
                    "package": v["PkgName"], "installed": v["InstalledVersion"], "type": r.get("Type"),
                    "id": v["VulnerabilityID"], "severity": sev, "fixed_in": v.get("FixedVersion") or "none",
                    "status": v.get("Status")}
    fixable_g = [x for x in g_hc.values() if x["fixed_in"] != "none"]
    fixable_t = [x for x in t_hc.values() if x["fixed_in"] != "none"]
    return {
        "grype": {"counts": {s: g_counts.get(s, 0) for s in SEV_ORDER}, "high_critical": len(g_hc),
                  "high_critical_with_fix": len(fixable_g),
                  "db_built": g.get("descriptor", {}).get("db", {}).get("status", {}).get("built")},
        "trivy": {"counts": {s: t_counts.get(s, 0) for s in SEV_ORDER}, "high_critical": len(t_hc),
                  "high_critical_with_fix": len(fixable_t),
                  "secrets": sum(len(r.get("Secrets") or []) for r in t.get("Results", []))},
        "grype_high_critical": sorted(g_hc.values(), key=lambda x: (x["package"], x["id"])),
        "trivy_high_critical": sorted(t_hc.values(), key=lambda x: (x["package"], x["id"])),
    }


PATTERNS = {
    "private-key-block": re.compile(rb"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY"),
    "aws-access-key-id": re.compile(rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "github-token": re.compile(rb"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b|github_pat_[A-Za-z0-9_]{50,}"),
    "slack-token": re.compile(rb"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    "openai-or-anthropic-key": re.compile(rb"\bsk-(?:ant-)?[A-Za-z0-9_-]{32,}"),
    "google-api-key": re.compile(rb"\bAIza[0-9A-Za-z_-]{35}\b"),
    "jwt": re.compile(rb"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    "url-with-credentials": re.compile(rb"\b[a-z][a-z0-9+.-]*://[^\s/:@'\"]{1,40}:[^\s/@'\"]{3,}@[A-Za-z0-9.-]+"),
}
SUSPICIOUS_NAMES = re.compile(
    r"(^|/)(\.env(\..*)?|\.netrc|\.npmrc|\.pypirc|id_rsa.*|id_ed25519.*|.*\.pem|.*\.key|.*\.p12|.*\.pfx|"
    r"credentials|\.git|\.git-credentials|\.aws|\.ssh|\.docker/config\.json|.*\.kdbx|.*\.gpg|.*\.sqlite3?)$")
BENIGN_NAME_PREFIXES = ("usr/share/ca-certificates/", "etc/ssl/certs/")


def cmd_secretscan(save_tar: str) -> dict:
    t, m, cfg, blob = _load_save(save_tar)
    res = {"layers": len(m["layers"]), "suspicious_file_names": [], "pattern_hits": [],
           "config_env_names": [e.split("=", 1)[0] for e in cfg["config"].get("Env", [])],
           "config_user": cfg["config"].get("User"), "healthcheck": cfg["config"].get("Healthcheck"),
           "history_created_by_secret_like": []}
    for h in cfg.get("history", []):
        cb = h.get("created_by", "")
        if re.search(r"(?i)(password|secret|token|api[_-]?key)\s*=\s*\S{6,}", cb):
            res["history_created_by_secret_like"].append(cb[:120])
    for i, layer in enumerate(m["layers"]):
        lt, members = _layer_entries(blob(layer["digest"]))
        for mem in members:
            name = mem.name
            if SUSPICIOUS_NAMES.search(name) and not name.startswith(BENIGN_NAME_PREFIXES) and not name.endswith("/.wh..wh..opq"):
                res["suspicious_file_names"].append({"layer": i, "path": name, "type": "dir" if mem.isdir() else "file"})
            if not mem.isfile() or mem.size == 0 or mem.size > 2_000_000:
                continue
            data = lt.extractfile(mem).read()
            if b"\x00" in data[:2048] and not name.endswith((".py", ".txt", ".json")):
                continue
            for pname, rx in PATTERNS.items():
                for hit in rx.finditer(data):
                    line = data.count(b"\n", 0, hit.start()) + 1
                    res["pattern_hits"].append({"layer": i, "path": name, "line": line, "pattern": pname})
                    break
    res["pattern_hit_counts"] = dict(Counter(h["pattern"] for h in res["pattern_hits"]))
    return res


def _flatten(x, prefix=""):
    if isinstance(x, dict):
        for k, v in x.items():
            yield from _flatten(v, f"{prefix}/{k}")
    elif isinstance(x, list):
        for i, v in enumerate(x):
            yield from _flatten(v, f"{prefix}[{i}]")
    else:
        yield prefix, x


def cmd_jsondiff(a: str, b: str, key: str) -> str:
    fa = dict(_flatten(json.load(open(a)).get(key, {})))
    fb = dict(_flatten(json.load(open(b)).get(key, {})))
    paths = [p for p in set(fa) | set(fb) if fa.get(p) != fb.get(p)]
    if not paths:
        return "(no differences)"
    norm = Counter(re.sub(r"step\d+:\d+(\[\d+\])*", "stepN", re.sub(r"sha256:[0-9a-f]{64}", "sha256:*", p)) for p in paths)
    return f"{len(paths)} values in {len(norm)} fields: " + "; ".join(f"{p} x{n}" if n > 1 else p for p, n in sorted(norm.items()))


def cmd_probe_projects(container: str, port: str) -> str:
    """The Projects image has no launch-code gate in this release: GET / and GET /api/projects on loopback."""
    import urllib.error
    import urllib.request
    base = f"http://127.0.0.1:{port}"

    def call(path):
        try:
            with urllib.request.urlopen(urllib.request.Request(base + path), timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    st0, _ = call("/")
    st1, b1 = call("/api/projects")
    return f"GET / {st0}; GET /api/projects {st1} ({len(b1)} bytes)"


def cmd_lint(path: str) -> dict:
    """Release-image Dockerfile policy: base pinned by digest, USER set, HEALTHCHECK present, no curl|sh."""
    text = open(path).read()
    froms = re.findall(r"^FROM\s+(\S+)", text, re.M)
    return {
        "file": path,
        "base_images": froms,
        "all_bases_digest_pinned": all("@sha256:" in f for f in froms),
        "has_user": bool(re.search(r"^USER\s+\S+", text, re.M)),
        "has_healthcheck": bool(re.search(r"^HEALTHCHECK\s+", text, re.M)),
        "curl_pipe_or_latest_download": bool(re.search(r"curl[^\n]*\|\s*(sh|bash|tar)|releases/latest|/main/", text)),
        "copy_dot": bool(re.search(r"^COPY\s+\.\s", text, re.M)),
        "oci_labels_set": all(f"org.opencontainers.image.{k}=" in text for k in ("title", "description", "version", "revision", "vendor")),
        "forbidden_labels_present": [k for k in ("source", "url", "documentation", "licenses") if f"org.opencontainers.image.{k}" in text],
        "pip_removed": "pip uninstall -y pip setuptools wheel" in text,
    }


FORBIDDEN_LABELS = ("source", "url", "documentation", "licenses", "authors", "base.name")


def cmd_config_check(tag: str, version: str, revision: str, title: str) -> dict:
    """docker inspect of the image: user, healthcheck, labels (exact expected values, none of the forbidden ones)."""
    info = json.loads(_run(["docker", "image", "inspect", tag]))[0]
    cfg = info["Config"]
    labels = cfg.get("Labels") or {}
    pre = "org.opencontainers.image."
    expect = {pre + "title": title, pre + "version": version, pre + "revision": revision, pre + "vendor": "ReDevOps"}
    problems = [f"{k}={labels.get(k)!r} expected {v!r}" for k, v in expect.items() if labels.get(k) != v]
    problems += [f"forbidden label {pre + k}" for k in FORBIDDEN_LABELS if (pre + k) in labels]
    if not labels.get(pre + "description"):
        problems.append("no description label")
    if cfg.get("User") != "10001:10001":
        problems.append(f"user {cfg.get('User')!r}")
    if not cfg.get("Healthcheck"):
        problems.append("no healthcheck")
    return {"image": tag, "user": cfg.get("User"), "healthcheck": cfg.get("Healthcheck", {}).get("Test"),
            "labels": {k: v for k, v in labels.items() if k.startswith(pre)}, "problems": problems}


def cmd_layer_scan(save_tar: str, topology_scanner: str) -> dict:
    """Private-identifier scan of EVERY file of EVERY layer (text files up to 3 MB) with the repository's own topology scanner
    (scripts/check_private_topology.py): private addresses, owner network, host/ssh aliases, MACs, home paths, personal mail,
    credentials; plus the literal personal login names and owner-host strings passed in OCI_EXTRA_NEEDLES (a file of lines).
    Also lists any .git, .env, key, pem file name. Reports file, line, category and the matched literal; no file content."""
    import importlib.util
    import os
    spec = importlib.util.spec_from_file_location("cpt", topology_scanner)
    cpt = importlib.util.module_from_spec(spec)
    sys.modules["cpt"] = cpt
    spec.loader.exec_module(cpt)
    needles = []
    nf = os.environ.get("OCI_EXTRA_NEEDLES")
    if nf and os.path.exists(nf):
        needles = [l.strip() for l in open(nf, encoding="utf-8") if l.strip()]
    t, m, cfg, blob = _load_save(save_tar)
    hits, names, scanned = [], [], 0
    name_rx = re.compile(r"(^|/)(\.git(/|$)|\.env($|\.)|id_rsa|id_ed25519|[^/]*\.(pem|key|p12|pfx)$|\.permissions\.key$|permissions\.env$)")
    for i, layer in enumerate(m["layers"]):
        lt, members = _layer_entries(blob(layer["digest"]))
        for mem in members:
            if name_rx.search(mem.name) and not mem.isdir() and "site-packages/certifi" not in mem.name:
                names.append({"layer": i, "path": mem.name})
            if not mem.isfile() or mem.size == 0 or mem.size > 3_000_000:
                continue
            data = lt.extractfile(mem).read()
            if b"\x00" in data[:4096]:
                continue
            text = data.decode("utf-8", "replace")
            scanned += 1
            for f in cpt.scan_text(mem.name, text):
                if f.category == "tech-term":
                    continue
                hits.append({"layer": i, "path": mem.name, "line": f.line, "category": f.category, "literal": f.literal})
            for nd in needles:
                if nd in text:
                    line = text[:text.index(nd)].count("\n") + 1
                    hits.append({"layer": i, "path": mem.name, "line": line, "category": "needle", "literal": nd})
    return {"layers": len(m["layers"]), "text_files_scanned": scanned, "hits": hits,
            "hit_counts": dict(Counter(h["category"] for h in hits)), "secret_like_file_names": names}


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    c, a = argv[1], argv[2:]
    if c == "digests":
        print(json.dumps(cmd_digests(*a), indent=2))
    elif c == "layerdiff":
        print(cmd_layerdiff(*a))
    elif c == "sbomcmp":
        print(json.dumps(cmd_sbomcmp(*a), indent=2))
    elif c == "vulnsummary":
        print(json.dumps(cmd_vulnsummary(*a), indent=2))
    elif c == "secretscan":
        print(json.dumps(cmd_secretscan(*a), indent=2))
    elif c == "jsondiff":
        print(cmd_jsondiff(*a))
    elif c == "probe-projects":
        print(cmd_probe_projects(*a))
    elif c == "dockerfile-lint":
        print(json.dumps(cmd_lint(*a), indent=2))
    elif c == "layer-scan":
        print(json.dumps(cmd_layer_scan(*a), indent=2))
    elif c == "config-check":
        print(json.dumps(cmd_config_check(*a), indent=2))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
