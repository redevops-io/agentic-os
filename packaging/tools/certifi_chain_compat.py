#!/usr/bin/env python3
"""certifi trust-chain compatibility test: does a host that validates with one CA bundle fail with another?

Purpose (owner decision 6): the R0-prime sidecar ships certifi 2026.10.19 instead of the frozen 2026.7.22 (advisory GHSA-6q48-6c7w-9gw2). The only
difference between the two bundles is the removal of `emSign ECC Root CA - G3`. This tool measures, with real read-only TLS handshakes, whether any host
the product contacts or documents, or any host of a broad sample, validates with the old bundle but fails with the new one. That is the only concrete
reason to change the certifi pin.

Sub-commands (standard library; `cryptography` is used only to name the chain's certificates when it is importable):

  fetch-bundles --out DIR [--version V ...]
        Download the certifi wheels from PyPI, check each wheel against the sha256 PyPI publishes, extract certifi/cacert.pem to
        DIR/cacert-<V>.pem and print the bundle sha256 and certificate count. Default versions: 2026.7.22 2026.10.19.
  extract-sidecar --sidecar FILE --out PEM        (needs PyInstaller importable)
        Pull certifi/cacert.pem out of a PyInstaller onefile sidecar, to prove the bytes that ship are the bytes that are tested.
  collect-hosts --out hosts.csv [--source LABEL=DIR ...] [--tranco FILE --top N --tld .in:N ...] [--extra FILE]
        Build the host list: hostnames found in `https://` literals under the source directories (product), the top-N Tranco ranks, the top-N hosts of a
        TLD list, explicit extra hosts.
  probe --hosts hosts.csv --bundle LABEL=PEM ... [--expect-sha256 LABEL=HEX ...] --out results.csv [--workers 16] [--timeout 8]
        One TLS handshake per host and bundle (SNI = host, hostname checking on, TLS >= 1.2, `cafile=` only: no system store unless a bundle IS the system file).
        No request is sent, no credential, nothing is posted: the connection is closed after the handshake.
  rerun-list --results results.csv --hosts hosts.csv --out rerun.csv
        Hosts whose verdict may have been spoiled by this machine (ephemeral-port exhaustion) or that reached TLS with some bundles only; probe them again with few workers.
  merge --base results.csv --override rerun-results.csv --out merged.csv
  report --results results.csv --old LABEL --new LABEL [--system LABEL] [--removed-root SUBSTRING ...] --out summary.json
        Summarise (JSON; exit 1 when there is a regression); list every host that validates with OLD but not NEW (the only finding that matters), the chains that end in the removed root, and the hosts that could not be judged.

Result classes per (host, bundle): OK (verified), VERIFY_FAIL (certificate verification error, with the OpenSSL code), TLS_ERROR (other TLS failure), NET_ERROR (DNS, connect, timeout:
no verdict). A host is JUDGED only when every bundle reached the TLS stage.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import ipaddress
import json
import re
import socket
import ssl
import sys
import time
import urllib.request
import warnings
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PYPI_JSON = "https://pypi.org/pypi/certifi/{v}/json"
URL_RE = re.compile(r"https?://([A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9])(?::\d+)?")
# hosts that appear in source as XML/JSON namespaces, badges or documentation links of other projects, or as placeholders: not endpoints the product contacts
NAMESPACE_HOSTS = {"www.w3.org", "json-schema.org", "schema.org", "img.shields.io", "example.com", "example.org", "example.net", "localhost",
                   "www.apache.org", "cyclonedx.org", "spdx.org", "purl.org", "tauri.app", "schema.tauri.app", "www.gnu.org", "opensource.org"}
LOCAL_SUFFIXES = (".local", ".svc", ".internal", ".invalid", ".test", ".example", ".localhost", ".lan", ".cluster.local")
TEXT_SUFFIXES = {".py", ".md", ".json", ".toml", ".yaml", ".yml", ".txt", ".sh", ".rs", ".cfg", ".ini", ".html", ".js"}
NON_FQDN_TLD = re.compile(r"\.(py|md|json|toml|js|css|png|svg|html|txt|yaml|yml|rs|so|dylib|pem)$", re.I)


# ----------------------------------------------------------------------------------------------- bundles
def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def count_certs(pem: bytes) -> int:
    return pem.count(b"-----BEGIN CERTIFICATE-----")


def pem_subjects(pem: bytes) -> list[str]:
    """`# Subject:` lines of a certifi-style bundle (the system bundle has none: empty list)."""
    return [m.group(1).decode("utf-8", "replace").strip() for m in re.finditer(rb"^# Subject: (.*)$", pem, re.M)]


def pem_cert_fingerprints(pem: bytes) -> set[str]:
    out = set()
    for m in re.finditer(rb"-----BEGIN CERTIFICATE-----(.*?)-----END CERTIFICATE-----", pem, re.S):
        out.add(sha256_bytes(base64.b64decode(b"".join(m.group(1).split()))))
    return out


def assert_bundle_sha256(label: str, path: Path, expected: str) -> None:
    got = sha256_bytes(Path(path).read_bytes())
    if got != expected.lower():
        raise SystemExit(f"bundle {label}: sha256 {got} != expected {expected.lower()} ({path})")


def fetch_bundle(version: str, out: Path) -> dict:
    """Download the wheel of one certifi release, verify it against PyPI's published sha256, extract cacert.pem."""
    with urllib.request.urlopen(PYPI_JSON.format(v=version), timeout=30) as r:
        meta = json.load(r)
    wheels = [u for u in meta["urls"] if u["filename"].endswith(".whl")]
    if len(wheels) != 1:
        raise SystemExit(f"certifi {version}: expected one wheel on PyPI, found {len(wheels)}")
    w = wheels[0]
    with urllib.request.urlopen(w["url"], timeout=60) as r:
        data = r.read()
    got = sha256_bytes(data)
    if got != w["digests"]["sha256"]:
        raise SystemExit(f"certifi {version}: wheel sha256 {got} != PyPI {w['digests']['sha256']}")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        pem = z.read("certifi/cacert.pem")
    path = out / f"cacert-{version}.pem"
    path.write_bytes(pem)
    return {"version": version, "wheel": w["filename"], "wheel_sha256": got, "uploaded": w.get("upload_time_iso_8601"),
            "bundle": str(path), "bundle_sha256": sha256_bytes(pem), "certificates": count_certs(pem)}


# ----------------------------------------------------------------------------------------------- hosts
def hosts_in_text(text: str) -> set[str]:
    found = set()
    for m in URL_RE.finditer(text):
        h = m.group(1).lower().rstrip(".")
        if "." not in h or h in NAMESPACE_HOSTS or h.endswith(LOCAL_SUFFIXES) or NON_FQDN_TLD.search(h):
            continue
        try:
            ipaddress.ip_address(h)
            continue                                  # an IP literal has no CA-validated name here
        except ValueError:
            pass
        if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", h):
            continue
        found.add(h)
    return found


def collect_from_dir(label: str, root: Path) -> dict[str, str]:
    """host -> first file (relative) that names it. Skips minified/vendored web bundles and binary files."""
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in TEXT_SUFFIXES or "projects_ui" in p.parts or "node_modules" in p.parts or "__pycache__" in p.parts:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for h in hosts_in_text(text):
            out.setdefault(h, f"{label}:{p.relative_to(root).as_posix()}")
    return out


def read_tranco(path: Path) -> list[str]:
    rows = []
    with open(path, newline="") as f:
        for r in csv.reader(f):
            if len(r) >= 2 and r[0].isdigit():
                rows.append(r[1].strip().lower())
    return rows


# ----------------------------------------------------------------------------------------------- probing
def _name_chain(der_list) -> list[dict]:
    try:
        from cryptography import x509
    except ImportError:
        return [{"fingerprint": sha256_bytes(d)} for d in der_list]
    out = []
    for d in der_list:
        try:
            with warnings.catch_warnings():            # some real roots carry a non-positive serial number; naming them is all we do
                warnings.simplefilter("ignore")
                c = x509.load_der_x509_certificate(d)
            out.append({"subject": c.subject.rfc4514_string(), "issuer": c.issuer.rfc4514_string(),
                        "not_after": c.not_valid_after_utc.strftime("%Y-%m-%d"), "fingerprint": sha256_bytes(d)})
        except Exception:                              # noqa: BLE001 - an unparsable certificate is recorded by fingerprint, never an error of the probe
            out.append({"fingerprint": sha256_bytes(d)})
    return out


def _der_chain(sslobj, verified: bool) -> list[bytes]:
    fn = getattr(sslobj, "get_verified_chain" if verified else "get_unverified_chain", None)
    if fn is None:
        return []
    try:
        certs = fn()
    except Exception:                                  # noqa: BLE001 - private API on 3.12; absent or failing means "no chain recorded"
        return []
    out = []
    for c in certs or []:
        try:
            out.append(c.public_bytes(ssl._ssl.ENCODING_DER) if hasattr(c, "public_bytes") else ssl.PEM_cert_to_DER_cert(c))
        except Exception:                              # noqa: BLE001
            return []
    return out


def probe_one(host: str, cafile: str, port: int = 443, timeout: float = 8.0) -> dict:
    ctx = ssl.create_default_context(cafile=cafile)    # cafile given: ONLY that file is loaded (no default store)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    t0 = time.monotonic()
    res = {"result": "NET_ERROR", "detail": "", "verify_code": "", "root": "", "chain_len": "", "leaf_issuer": "", "leaf_not_after": "", "tls": "", "ms": ""}
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except (OSError, UnicodeError) as e:
        res["detail"] = f"{type(e).__name__}: {e}"[:160]
        return res
    try:
        with sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ss:
                ss.settimeout(timeout)
                chain = _name_chain(_der_chain(ss._sslobj, True))
                res.update(result="OK", tls=ss.version() or "")
                if chain:
                    res.update(root=chain[-1].get("subject", chain[-1].get("fingerprint", "")), chain_len=str(len(chain)),
                               leaf_issuer=chain[0].get("issuer", ""), leaf_not_after=chain[0].get("not_after", ""))
    except ssl.SSLCertVerificationError as e:
        res.update(result="VERIFY_FAIL", verify_code=str(e.verify_code), detail=(e.verify_message or str(e))[:160])
    except ssl.SSLError as e:
        res.update(result="TLS_ERROR", detail=f"{type(e).__name__}: {e}"[:160])
    except (OSError, UnicodeError) as e:
        res.update(result="NET_ERROR", detail=f"{type(e).__name__}: {e}"[:160])
    res["ms"] = str(int((time.monotonic() - t0) * 1000))
    return res


def peer_chain_unverified(host: str, port: int = 443, timeout: float = 8.0) -> list[dict]:
    """The chain a server presents, fetched WITHOUT verification, only to name what a failing host sends (never used to trust anything)."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock, ctx.wrap_socket(sock, server_hostname=host) as ss:
            return _name_chain(_der_chain(ss._sslobj, False))
    except (OSError, ssl.SSLError, UnicodeError):
        return []


def probe_host(host: str, bundles: dict[str, str], timeout: float, attempts: int = 2) -> dict[str, dict]:
    """All bundles for one host.

    The first bundle is probed first: when the host cannot be reached at all (NET_ERROR) it is retried once and, if it still cannot be reached,
    every bundle gets the same NET_ERROR row without further attempts (an unreachable host costs one timeout, not one per bundle). A reachable host
    is probed with every bundle; a disagreement between bundles or a NET_ERROR on one of them is retried once, so transient network noise cannot look
    like a trust difference.
    """
    labels = list(bundles)
    first = probe_one(host, bundles[labels[0]], timeout=timeout)
    for _ in range(attempts - 1):
        if first["result"] != "NET_ERROR":
            break
        time.sleep(0.5)
        first = probe_one(host, bundles[labels[0]], timeout=timeout)
    if first["result"] == "NET_ERROR":
        return {lb: dict(first) for lb in labels}
    out: dict[str, dict] = {labels[0]: first}
    for lb in labels[1:]:
        out[lb] = probe_one(host, bundles[lb], timeout=timeout)
    for _ in range(attempts - 1):
        classes = {r["result"] for r in out.values()}
        if "NET_ERROR" not in classes and len(classes) == 1:
            break
        time.sleep(0.5)
        out = {lb: probe_one(host, bundles[lb], timeout=timeout) for lb in labels}
    return out


FIELDS = ["host", "category", "detail_source", "bundle", "result", "verify_code", "detail", "root", "chain_len", "leaf_issuer", "leaf_not_after", "tls", "ms"]


def run_probe(hosts: list[dict], bundles: dict[str, str], timeout: float, workers: int, progress=None) -> list[dict]:
    rows: list[dict] = []

    def work(h):
        res = probe_host(h["host"], bundles, timeout)
        return [{"host": h["host"], "category": h["category"], "detail_source": h.get("detail", ""), "bundle": b, **r} for b, r in res.items()]

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, got in enumerate(ex.map(work, hosts), 1):
            rows.extend(got)
            if progress and i % 250 == 0:
                progress(i, len(hosts))
    return rows


# ----------------------------------------------------------------------------------------------- comparison
def by_host(rows: list[dict]) -> dict[str, dict[str, dict]]:
    d: dict[str, dict[str, dict]] = {}
    for r in rows:
        d.setdefault(r["host"], {})[r["bundle"]] = r
    return d


def judged(per_bundle: dict[str, dict]) -> bool:
    return all(r["result"] != "NET_ERROR" for r in per_bundle.values())


def regressions(rows: list[dict], old: str, new: str) -> list[str]:
    """Hosts that validate with OLD and do not validate with NEW (both reached the TLS stage). This is the finding that matters."""
    out = []
    for h, b in by_host(rows).items():
        if old in b and new in b and b[old]["result"] == "OK" and b[new]["result"] in ("VERIFY_FAIL", "TLS_ERROR"):
            out.append(h)
    return sorted(out)


def improvements(rows: list[dict], old: str, new: str) -> list[str]:
    out = []
    for h, b in by_host(rows).items():
        if old in b and new in b and b[old]["result"] in ("VERIFY_FAIL", "TLS_ERROR") and b[new]["result"] == "OK":
            out.append(h)
    return sorted(out)


def summarise(rows: list[dict], old: str, new: str, system: str | None, removed_roots: list[str]) -> dict:
    hosts = by_host(rows)
    labels = sorted({r["bundle"] for r in rows})
    cats: dict[str, int] = {}
    for h, b in hosts.items():
        cats[next(iter(b.values()))["category"]] = cats.get(next(iter(b.values()))["category"], 0) + 1
    tally = {lb: {} for lb in labels}
    for r in rows:
        tally[r["bundle"]][r["result"]] = tally[r["bundle"]].get(r["result"], 0) + 1
    on_removed = sorted(h for h, b in hosts.items() if old in b and b[old]["result"] == "OK" and any(rt and rt in b[old]["root"] for rt in removed_roots))
    both_judged = [h for h, b in hosts.items() if judged(b)]
    sys_only_ok = sorted(h for h, b in hosts.items() if system and system in b and old in b and b[system]["result"] == "OK" and b[old]["result"] == "VERIFY_FAIL")
    return {"hosts": len(hosts), "judged": len(both_judged), "categories": cats, "tally": tally, "regressions": regressions(rows, old, new),
            "improvements": improvements(rows, old, new), "chains_ending_in_removed_roots": on_removed, "ok_with_system_only": sys_only_ok}


LOCAL_ERRORS = ("Can't assign requested address", "Too many open files", "Address already in use", "No buffer space")


def rerun_candidates(rows: list[dict]) -> list[str]:
    """Hosts whose verdict may have been spoiled by THIS machine (ephemeral-port or descriptor exhaustion under a high worker count) or that reached the
    TLS stage with one bundle and not with another: they are probed again, slowly, and the new rows replace the old ones (`merge`)."""
    out = []
    for h, b in by_host(rows).items():
        classes = {r["result"] for r in b.values()}
        local = any(r["result"] == "NET_ERROR" and any(e in r["detail"] for e in LOCAL_ERRORS) for r in b.values())
        if local or ("NET_ERROR" in classes and len(classes) > 1):
            out.append(h)
    return sorted(out)


def merge_rows(base: list[dict], override: list[dict]) -> list[dict]:
    """Rows of `override` replace every row of the same host in `base`."""
    hosts = {r["host"] for r in override}
    return [r for r in base if r["host"] not in hosts] + override


# ----------------------------------------------------------------------------------------------- CLI
def _kv(items: list[str]) -> dict[str, str]:
    out = {}
    for it in items:
        k, _, v = it.partition("=")
        if not k or not v:
            raise SystemExit(f"expected LABEL=VALUE, got {it!r}")
        out[k] = v
    return out


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("fetch-bundles")
    p.add_argument("--out", required=True)
    p.add_argument("--version", action="append")
    p = sub.add_parser("extract-sidecar")
    p.add_argument("--sidecar", required=True)
    p.add_argument("--out", required=True)
    p = sub.add_parser("collect-hosts")
    p.add_argument("--out", required=True)
    p.add_argument("--source", action="append", default=[], help="LABEL=DIR: hostnames in https:// literals under DIR (category product:LABEL)")
    p.add_argument("--tranco")
    p.add_argument("--top", type=int, default=0)
    p.add_argument("--tld", action="append", default=[], help="SUFFIX:N, e.g. .in:1500 (the first N Tranco ranks ending in SUFFIX)")
    p.add_argument("--extra", help="file with one hostname per line; optional '# category' header lines")
    p = sub.add_parser("probe")
    p.add_argument("--hosts", required=True)
    p.add_argument("--bundle", action="append", required=True)
    p.add_argument("--expect-sha256", action="append", default=[])
    p.add_argument("--out", required=True)
    p.add_argument("--workers", type=int, default=16)
    p.add_argument("--timeout", type=float, default=8.0)
    p = sub.add_parser("rerun-list", help="write the hosts to probe again (local network errors, or reached with some bundles only) as a hosts file")
    p.add_argument("--results", required=True)
    p.add_argument("--hosts", required=True)
    p.add_argument("--out", required=True)
    p = sub.add_parser("merge", help="rows of --override replace the rows of the same hosts in --base")
    p.add_argument("--base", required=True)
    p.add_argument("--override", required=True)
    p.add_argument("--out", required=True)
    p = sub.add_parser("report")
    p.add_argument("--results", required=True)
    p.add_argument("--old", required=True)
    p.add_argument("--new", required=True)
    p.add_argument("--system")
    p.add_argument("--removed-root", action="append", default=[])
    p.add_argument("--out", required=True)
    a = ap.parse_args(argv)

    if a.cmd == "fetch-bundles":
        out = Path(a.out)
        out.mkdir(parents=True, exist_ok=True)
        for v in a.version or ["2026.7.22", "2026.10.19"]:
            print(json.dumps(fetch_bundle(v, out)))
        return 0
    if a.cmd == "extract-sidecar":
        from PyInstaller.archive.readers import CArchiveReader
        rd = CArchiveReader(a.sidecar)
        names = [n for n in rd.toc if n.endswith("cacert.pem")]
        if len(names) != 1:
            raise SystemExit(f"expected exactly one cacert.pem in the archive, found {names}")
        d = rd.extract(names[0])
        d = d[-1] if isinstance(d, tuple) else d
        Path(a.out).write_bytes(d)
        print(json.dumps({"entry": names[0], "sha256": sha256_bytes(d), "bytes": len(d), "certificates": count_certs(d)}))
        return 0
    if a.cmd == "collect-hosts":
        seen: dict[str, tuple[str, str]] = {}
        for lb, d in _kv(a.source).items():
            for h, src in collect_from_dir(lb, Path(d)).items():
                seen.setdefault(h, (f"product:{lb}", src))
        if a.tranco:
            ranks = read_tranco(Path(a.tranco))
            for i, h in enumerate(ranks[:a.top], 1):
                seen.setdefault(h, ("tranco-top", f"rank {i}"))
            for spec in a.tld:
                suf, _, n = spec.rpartition(":")
                got = 0
                for i, h in enumerate(ranks, 1):
                    if h.endswith(suf):
                        if seen.setdefault(h, (f"tranco-tld{suf}", f"rank {i}"))[0] == f"tranco-tld{suf}":
                            got += 1
                        if got >= int(n):
                            break
        if a.extra:
            cat = "extra"
            for line in Path(a.extra).read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("#"):
                    cat = line.lstrip("# ").split(" (")[0].strip() or "extra"      # "# name (free text)" -> name
                elif line:
                    seen[line.lower()] = (cat, "extra list")          # an explicit entry wins over a rank entry
        write_csv(Path(a.out), ["host", "category", "detail"], [{"host": h, "category": c, "detail": d} for h, (c, d) in sorted(seen.items())])
        print(f"{len(seen)} hosts -> {a.out}")
        return 0
    if a.cmd == "probe":
        bundles = _kv(a.bundle)
        for lb, h in _kv(a.expect_sha256).items():
            assert_bundle_sha256(lb, Path(bundles[lb]), h)
        for lb, pth in bundles.items():
            data = Path(pth).read_bytes()
            print(f"bundle {lb}: {pth} sha256 {sha256_bytes(data)} certificates {count_certs(data)}", file=sys.stderr)
        with open(a.hosts, newline="") as f:
            hosts = list(csv.DictReader(f))
        rows = run_probe(hosts, bundles, a.timeout, a.workers, lambda i, n: print(f"  {i}/{n} hosts", file=sys.stderr))
        write_csv(Path(a.out), FIELDS, rows)
        print(f"{len(rows)} rows ({len(hosts)} hosts x {len(bundles)} bundles) -> {a.out}")
        return 0
    if a.cmd == "rerun-list":
        with open(a.results, newline="") as f:
            names = set(rerun_candidates(list(csv.DictReader(f))))
        with open(a.hosts, newline="") as f:
            keep = [r for r in csv.DictReader(f) if r["host"] in names]
        write_csv(Path(a.out), ["host", "category", "detail"], keep)
        print(f"{len(keep)} hosts to probe again -> {a.out}")
        return 0
    if a.cmd == "merge":
        with open(a.base, newline="") as f:
            base = list(csv.DictReader(f))
        with open(a.override, newline="") as f:
            over = list(csv.DictReader(f))
        merged = merge_rows(base, over)
        write_csv(Path(a.out), FIELDS, merged)
        print(f"{len(merged)} rows ({len({r['host'] for r in over})} hosts replaced) -> {a.out}")
        return 0
    if a.cmd == "report":
        with open(a.results, newline="") as f:
            rows = list(csv.DictReader(f))
        s = summarise(rows, a.old, a.new, a.system, a.removed_root)
        Path(a.out).write_text(json.dumps(s, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({k: (v if not isinstance(v, list) else len(v)) for k, v in s.items() if k != "tally"}))
        return 1 if s["regressions"] else 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
