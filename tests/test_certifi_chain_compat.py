"""packaging/tools/certifi_chain_compat.py: the certifi trust-chain compatibility tool (owner decision 6).

Offline: a local CA, a leaf for `localhost` and a TLS server on 127.0.0.1 stand in for the internet, so the tool's verdicts (OK, VERIFY_FAIL,
NET_ERROR), its chain/root naming, its host extraction and its "validates with the old bundle but not the new one" detector are tested without a
network. The live measurement itself (real hosts, the two real bundles) is a command run, recorded in packaging/evidence/certifi-chain-compat-2026-11-04.md.
An opt-in test (`CERTIFI_COMPAT_BUNDLES=<dir with cacert-2026.7.22.pem and cacert-2026.10.19.pem>`) pins the known difference between the two real bundles.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import socket
import ssl
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packaging" / "tools"))
import certifi_chain_compat as cc  # noqa: E402

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402


def _name(cn: str):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _ca(cn: str):
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(_name(cn)).issuer_name(_name(cn)).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
            .sign(key, hashes.SHA256()))
    return key, cert


def _leaf(ca_key, ca_cert, dns: str):
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(_name(dns)).issuer_name(ca_cert.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=10))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(dns)]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(ca_key, hashes.SHA256()))
    return key, cert


def _pem(cert) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def _certifi_style(cert) -> bytes:
    cn = cert.subject.rfc4514_string()
    return (f"# Issuer: {cn}\n# Subject: {cn}\n# Label: \"{cn}\"\n").encode() + _pem(cert)


class _Server:
    """A TLS server on 127.0.0.1 presenting `leaf` (and nothing else); handshake only, then close."""

    def __init__(self, tmp: Path, leaf_key, leaf_cert):
        (tmp / "leaf.pem").write_bytes(_pem(leaf_cert))
        (tmp / "leaf.key").write_bytes(leaf_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(tmp / "leaf.pem", tmp / "leaf.key")
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(32)
        self.port = self.sock.getsockname()[1]
        self.stop = False
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self):
        self.sock.settimeout(0.2)
        while not self.stop:
            try:
                c, _ = self.sock.accept()
            except OSError:
                continue
            try:
                with self.ctx.wrap_socket(c, server_side=True) as s:
                    s.settimeout(1)
            except (ssl.SSLError, OSError):
                pass

    def close(self):
        self.stop = True
        self.t.join(2)
        self.sock.close()


@pytest.fixture()
def world(tmp_path):
    """CA_OLD (in the old bundle only) signs the leaf; CA_OTHER is in both bundles."""
    old_key, old_ca = _ca("Old Root CA - G3")
    oth_key, oth_ca = _ca("Other Root CA")
    leaf_key, leaf = _leaf(old_key, old_ca, "localhost")
    old_bundle = tmp_path / "old.pem"
    new_bundle = tmp_path / "new.pem"
    old_bundle.write_bytes(_certifi_style(old_ca) + _certifi_style(oth_ca))
    new_bundle.write_bytes(_certifi_style(oth_ca))
    srv = _Server(tmp_path, leaf_key, leaf)
    yield {"old": str(old_bundle), "new": str(new_bundle), "port": srv.port, "ca": old_ca, "tmp": tmp_path}
    srv.close()


# ---------------------------------------------------------------------------------------------- probing
def test_probe_ok_with_the_bundle_that_holds_the_root_and_names_the_chain(world):
    r = cc.probe_one("localhost", world["old"], port=world["port"], timeout=5)
    assert r["result"] == "OK" and r["tls"].startswith("TLSv1")
    assert r["root"] == "CN=Old Root CA - G3" and r["chain_len"] == "2" and r["leaf_issuer"] == "CN=Old Root CA - G3"


def test_probe_verify_fail_when_the_root_is_missing_from_the_bundle(world):
    r = cc.probe_one("localhost", world["new"], port=world["port"], timeout=5)
    assert r["result"] == "VERIFY_FAIL" and r["verify_code"] == "20"         # unable to get local issuer certificate
    assert "issuer" in r["detail"]


def test_probe_checks_the_host_name(world):
    r = cc.probe_one("127.0.0.1", world["old"], port=world["port"], timeout=5)   # the leaf only names localhost
    assert r["result"] == "VERIFY_FAIL" and r["verify_code"] in ("62", "64")      # hostname / IP address mismatch


def test_probe_closed_port_and_unresolvable_name_are_net_errors_not_trust_verdicts(world):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert cc.probe_one("localhost", world["old"], port=port, timeout=2)["result"] == "NET_ERROR"
    assert cc.probe_one("no-such-host.invalid", world["old"], timeout=2)["result"] == "NET_ERROR"


def test_probe_uses_only_the_given_file_not_the_system_store(world):
    # a bundle with an unrelated root must not fall back to a default store
    only_other = world["tmp"] / "only-other.pem"
    only_other.write_bytes(Path(world["new"]).read_bytes())
    assert cc.probe_one("localhost", str(only_other), port=world["port"], timeout=5)["result"] == "VERIFY_FAIL"


def test_peer_chain_is_named_without_trusting_it(world):
    chain = cc.peer_chain_unverified("localhost", port=world["port"], timeout=5)
    assert chain and chain[0]["issuer"] == "CN=Old Root CA - G3"


# ---------------------------------------------------------------------------------------------- comparison
def _rows(world):
    hosts = [{"host": "localhost", "category": "test", "detail": ""}]
    rows = []
    for label, path in (("old", world["old"]), ("new", world["new"])):
        r = cc.probe_one("localhost", path, port=world["port"], timeout=5)
        rows.append({"host": "localhost", "category": "test", "detail_source": "", "bundle": label, **r})
    return hosts, rows


def test_a_host_that_validates_with_the_old_bundle_but_not_the_new_one_is_reported(world):
    _, rows = _rows(world)
    assert cc.regressions(rows, "old", "new") == ["localhost"]
    assert cc.improvements(rows, "old", "new") == []
    s = cc.summarise(rows, "old", "new", None, ["Old Root CA - G3"])
    assert s["regressions"] == ["localhost"] and s["chains_ending_in_removed_roots"] == ["localhost"] and s["judged"] == 1
    assert cc.regressions(rows, "new", "old") == [] and cc.improvements(rows, "new", "old") == ["localhost"]


def test_run_probe_end_to_end_and_csv_round_trip(world, tmp_path):
    rows = cc.run_probe([{"host": "localhost", "category": "test", "detail": "x"}], {"old": world["old"], "new": world["new"]}, 5, 2)
    # run_probe uses port 443; the local server is elsewhere, so both are NET_ERROR: not a verdict, never a regression
    assert {r["result"] for r in rows} == {"NET_ERROR"} and cc.regressions(rows, "old", "new") == []
    out = tmp_path / "r.csv"
    cc.write_csv(out, cc.FIELDS, rows)
    import csv
    back = list(csv.DictReader(open(out)))
    assert [r["bundle"] for r in back] == ["old", "new"] and back[0]["host"] == "localhost"


def test_an_unreachable_host_costs_one_probe_and_a_retry_not_one_per_bundle(monkeypatch):
    calls = []

    def fake(host, cafile, port=443, timeout=8.0):
        calls.append(cafile)
        return {"result": "NET_ERROR", "detail": "timeout"}

    monkeypatch.setattr(cc, "probe_one", fake)
    monkeypatch.setattr(cc.time, "sleep", lambda s: None)
    out = cc.probe_host("h", {"a": "A", "b": "B", "c": "C"}, 1)
    assert set(out) == {"a", "b", "c"} and {r["result"] for r in out.values()} == {"NET_ERROR"}
    assert calls == ["A", "A"]                                   # first bundle only, one retry


def test_a_disagreement_between_bundles_is_re_probed_once(monkeypatch):
    seq = {"A": iter(["OK", "OK"]), "B": iter(["VERIFY_FAIL", "VERIFY_FAIL"])}
    monkeypatch.setattr(cc, "probe_one", lambda host, cafile, port=443, timeout=8.0: {"result": next(seq[cafile])})
    monkeypatch.setattr(cc.time, "sleep", lambda s: None)
    out = cc.probe_host("h", {"old": "A", "new": "B"}, 1)
    assert out["old"]["result"] == "OK" and out["new"]["result"] == "VERIFY_FAIL"
    assert list(seq["A"]) == [] and list(seq["B"]) == []          # both consumed twice: probed, then re-probed


def test_rerun_list_and_merge_replace_only_the_spoiled_hosts():
    def row(h, b, res, detail=""):
        return {"host": h, "category": "c", "bundle": b, "result": res, "detail": detail, "root": ""}
    base = [row("good", "old", "OK"), row("good", "new", "OK"),
            row("local", "old", "NET_ERROR", "OSError: [Errno 49] Can't assign requested address"), row("local", "new", "NET_ERROR", "OSError: [Errno 49] Can't assign requested address"),
            row("mixed", "old", "OK"), row("mixed", "new", "NET_ERROR", "timed out"),
            row("dead", "old", "NET_ERROR", "gaierror"), row("dead", "new", "NET_ERROR", "gaierror")]
    assert cc.rerun_candidates(base) == ["local", "mixed"]          # a host that is dead for every bundle is not re-probed
    new = [row("local", "old", "OK"), row("local", "new", "VERIFY_FAIL"), row("mixed", "old", "OK"), row("mixed", "new", "OK")]
    merged = cc.merge_rows(base, new)
    assert len(merged) == 8 and [r["result"] for r in merged if r["host"] == "local"] == ["OK", "VERIFY_FAIL"]
    assert cc.regressions(merged, "old", "new") == ["local"]


def test_net_error_rows_are_not_judged_and_never_count_as_regressions():
    rows = [{"host": "h", "category": "c", "bundle": "old", "result": "OK", "root": "r"},
            {"host": "h", "category": "c", "bundle": "new", "result": "NET_ERROR", "root": ""}]
    assert cc.regressions(rows, "old", "new") == []
    assert not cc.judged(cc.by_host(rows)["h"])


def test_a_failure_with_both_bundles_is_not_a_regression():
    rows = [{"host": "h", "category": "c", "bundle": "old", "result": "VERIFY_FAIL", "root": ""},
            {"host": "h", "category": "c", "bundle": "new", "result": "VERIFY_FAIL", "root": ""}]
    assert cc.regressions(rows, "old", "new") == [] and cc.improvements(rows, "old", "new") == []


# ---------------------------------------------------------------------------------------------- hosts and bundles
def test_host_extraction_keeps_real_endpoints_and_drops_namespaces_ips_and_placeholders():
    text = ('url = "https://api.hubapi.com/crm/v3" ; "http://www.w3.org/2000/svg" ; https://127.0.0.1:8787/ ; http://localhost:3000 ; '
            'https://example.com/x ; https://dagster-webserver.dagster.svc:3000 ; https://sub.Example-Corp.io:8443/p?q=1 ; https://README.md ; '
            'https://{host}/path ; https://img.shields.io/badge ; https://192.168.40.201:8080 ; https://singlelabel/')
    assert cc.hosts_in_text(text) == {"api.hubapi.com", "sub.example-corp.io"}


def test_collect_from_dir_skips_the_minified_ui_bundle_and_binary_files(tmp_path):
    (tmp_path / "a.py").write_text("X = 'https://api.openai.com/v1'\n")
    (tmp_path / "projects_ui").mkdir()
    (tmp_path / "projects_ui" / "index.js").write_text("https://reactjs.org/docs")
    (tmp_path / "blob.bin").write_bytes(b"https://binary.example.net\xff\xfe")
    got = cc.collect_from_dir("t", tmp_path)
    assert got == {"api.openai.com": "t:a.py"}


def test_bundle_helpers_and_the_sha256_assertion(tmp_path):
    key, ca = _ca("Some Root")
    pem = _certifi_style(ca) + _certifi_style(_ca("Another Root")[1])
    p = tmp_path / "b.pem"
    p.write_bytes(pem)
    assert cc.count_certs(pem) == 2 and cc.pem_subjects(pem) == ["CN=Some Root", "CN=Another Root"]
    assert len(cc.pem_cert_fingerprints(pem)) == 2
    cc.assert_bundle_sha256("b", p, hashlib.sha256(pem).hexdigest())
    with pytest.raises(SystemExit):
        cc.assert_bundle_sha256("b", p, "0" * 64)


def test_cli_probe_refuses_a_bundle_with_the_wrong_sha256(tmp_path):
    hosts = tmp_path / "h.csv"
    hosts.write_text("host,category,detail\nlocalhost,t,\n")
    b = tmp_path / "b.pem"
    b.write_bytes(_certifi_style(_ca("R")[1]))
    with pytest.raises(SystemExit) as e:
        cc.main(["probe", "--hosts", str(hosts), "--bundle", f"x={b}", "--expect-sha256", "x=" + "0" * 64, "--out", str(tmp_path / "o.csv")])
    assert "sha256" in str(e.value)


# ---------------------------------------------------------------------------------------------- the real bundles (opt-in, offline once downloaded)
@pytest.mark.skipif(not os.environ.get("CERTIFI_COMPAT_BUNDLES"), reason="set CERTIFI_COMPAT_BUNDLES to a directory with cacert-2026.7.22.pem and cacert-2026.10.19.pem")
def test_the_only_difference_between_the_frozen_and_the_pinned_bundle_is_emsign_ecc_g3():
    d = Path(os.environ["CERTIFI_COMPAT_BUNDLES"])
    old, new = (d / "cacert-2026.7.22.pem").read_bytes(), (d / "cacert-2026.10.19.pem").read_bytes()
    assert hashlib.sha256(old).hexdigest() == "9cc2a774b5198dcff14d9be1e66091f538975d867ce029a96bce15a55dfd730f"
    assert hashlib.sha256(new).hexdigest() == "50f79f98dac192b20dbf73f35c56f0b0c557ab267ff5199d1117cc1180b70785"
    assert cc.count_certs(old) == 121 and cc.count_certs(new) == 120
    fo, fn = cc.pem_cert_fingerprints(old), cc.pem_cert_fingerprints(new)
    assert fn < fo and len(fo - fn) == 1 and not (fn - fo)
    removed = [s for s in cc.pem_subjects(old) if s not in set(cc.pem_subjects(new))]
    assert len(removed) == 1 and removed[0].startswith("CN=emSign ECC Root CA - G3")
