"""Regression tests for the independent review of #458 (each fails on the previous code)."""
from __future__ import annotations

import hashlib
import io
import os
import time
import zipfile

import pytest

APPROVAL = {"approved": True, "by": "alice"}


# ── 1. case-insensitive file systems: one identity per document ───────────────────────────────────────────────
def test_ids_that_differ_only_by_case_share_one_identity(env, mods, kit):
    S = mods.store
    assert S.canonical_id("Secret.docx") == S.canonical_id("secret.docx") == S.canonical_id("SECRET.DOCX")
    assert S.LocalFSStore._key("Secret.docx") == S.LocalFSStore._key("sECRET.docx")
    assert S.canonical_id("é.docx") == S.canonical_id("É.docx")


def test_label_and_versions_follow_the_real_file_whatever_case_is_asked(env, kit):
    """Emulates a case-insensitive FS: only 'Secret.docx' exists, the caller asks for 'secret.docx'."""
    env.put("acme", "Secret.docx", kit.make_docx(["x"]), label="RESTRICTED")
    assert env.store.get_classification("acme", "secret.docx") == "RESTRICTED"
    assert env.store.head("acme", "SECRET.DOCX").sha256 == env.store.head("acme", "Secret.docx").sha256
    # the RESTRICTED label cannot be bypassed by changing the case of the id
    with pytest.raises(env.mods.errors.PolicyDenied, match="never sent"):
        env.svc.convert("acme", "secret.docx", "pdf")
    assert env.backend.calls == []


def test_version_counter_and_lock_are_shared_across_case_variants(env, kit):
    env.put("acme", "Report.docx", kit.make_docx(["x"]))
    env.store.head("acme", "Report.docx")
    env.store.put_version("acme", "report.docx", b"two", expected_version=1, actor="a", kind="edit")
    with pytest.raises(env.mods.errors.StaleVersion):                       # the stale guard sees v2 via any case
        env.store.put_version("acme", "REPORT.docx", b"three", expected_version=1, actor="b", kind="edit")
    assert [v.version for v in env.store.versions("acme", "rEpOrT.docx")] == [1, 2]
    assert env.store._lock("acme", "Report.docx").path == env.store._lock("acme", "report.DOCX").path


def test_two_entries_differing_only_by_case_are_refused_as_ambiguous(env, kit):
    p = env.put("acme", "Secret.docx", kit.make_docx(["x"]))
    alias = p.with_name("secret.docx")
    try:
        os.link(p, alias)                                  # on a case-INSENSITIVE fs this raises: already the same file
    except OSError:
        pytest.skip("hardlinks unavailable")
    if len(os.listdir(p.parent)) < 2:
        pytest.skip("case-insensitive file system: the alias IS the same entry (identity already shared)")
    with pytest.raises(env.mods.errors.InvalidPath, match="ambiguous"):
        env.store.head("acme", "Secret.docx")


# ── 9. Windows reserved names with space / dot variants ─────────────────────────────────────────────────────
@pytest.mark.parametrize("bad", ["con .docx", "CON .txt", "CON.", "nul.txt", "COM1.x", "dir/lpt1 .docx", "aux  .a.b",
                                 "Prn.tar.gz", "COM² .docx"])
def test_reserved_device_names_in_every_variant_are_rejected(mods, bad):
    with pytest.raises(mods.errors.InvalidPath):
        mods.store.safe_rel_path(bad)


# ── 2. idempotency key reuse with a different payload ───────────────────────────────────────────────────────
def test_reusing_an_idempotency_key_for_a_different_proposal_is_a_conflict(env, kit):
    env.put("acme", "a.docx", kit.make_docx(["Price 100 EUR", "Other 5 USD"]), label="PUBLIC")
    p1 = env.svc.propose_patch("acme", "a.docx", [{"type": "replace_text", "search": "100 EUR", "replace": "1 EUR"}])
    p2 = env.svc.propose_patch("acme", "a.docx", [{"type": "replace_text", "search": "5 USD", "replace": "6 USD"}])

    def go(p):
        return env.svc.apply_patch("acme", proposal_id=p["proposal_id"], base_hash=p["base_hash"], backend=p["backend"],
                                   patch_digest=p["patch_digest"], expected_version=p["base_version"],
                                   idempotency_key="idem-shared-key-1", approval=APPROVAL)
    assert go(p1)["status"] == "ok"
    with pytest.raises(env.mods.errors.Conflict, match="different proposal"):
        go(p2)
    assert env.backend.calls.count("apply_edit") == 1


def test_apply_rederives_the_digest_from_the_stored_ops(env, kit):
    import json
    env.put("acme", "a.docx", kit.make_docx(["Price 100 EUR"]), label="PUBLIC")
    p = env.svc.propose_patch("acme", "a.docx", [{"type": "replace_text", "search": "100 EUR", "replace": "1 EUR"}])
    f = env.state / "proposals" / "acme" / f"{p['proposal_id']}.json"
    d = json.loads(f.read_text())
    d["ops"][0]["replace"] = "1000000 EUR"                    # doctored proposal file, digest field untouched
    f.write_text(json.dumps(d))
    with pytest.raises(env.mods.errors.Conflict, match="digest"):
        env.svc.apply_patch("acme", proposal_id=p["proposal_id"], base_hash=p["base_hash"], backend=p["backend"],
                            patch_digest=p["patch_digest"], expected_version=1, idempotency_key="idem-tamper-0001",
                            approval=APPROVAL)
    assert env.backend.calls == []


# ── 3. validate + cap before EVERY transmit; breaker; concurrency ──────────────────────────────────────────────
def junk_pk():
    return b"PK\x03\x04" + b"junk" * 50


@pytest.mark.parametrize("op", ["convert", "export", "open", "inspect"])
def test_junk_pk_bytes_never_reach_the_editor(env, op):
    # NOTE: the "inspect" case also passed before the review fixes (inspect already validated); convert/export/open are new.
    env.put("acme", "j.docx", junk_pk(), label="PUBLIC")
    fn = {"convert": lambda: env.svc.convert("acme", "j.docx", "pdf"),
          "export": lambda: env.svc.export("acme", "j.docx", "pdf"),
          "open": lambda: env.svc.open_session("acme", "j.docx", user_id="u"),
          "inspect": lambda: env.svc.inspect("acme", "j.docx")}[op]
    with pytest.raises(env.mods.errors.InvalidInput):
        fn()
    assert env.backend.calls == []


def test_oversized_documents_are_not_transmitted(env, kit):
    env.put("acme", "big.docx", kit.make_docx([os.urandom(2000).hex()]), label="PUBLIC")
    env.svc.max_source_bytes = 1000
    with pytest.raises(env.mods.errors.InvalidInput, match="larger"):
        env.svc.convert("acme", "big.docx", "pdf")
    assert env.backend.calls == []


def test_store_caps_file_reads(mods, tmp_path):
    (tmp_path / "acme").mkdir()
    (tmp_path / "acme" / "h.docx").write_bytes(b"x" * 5000)
    st = mods.store.LocalFSStore(tmp_path, max_bytes=1000)
    with pytest.raises(mods.errors.InvalidPath, match="limit"):
        st.head("acme", "h.docx")


def test_circuit_breaker_opens_after_repeated_editor_failures(env, kit):
    env.put("acme", "p.docx", kit.make_docx(["x"]), label="PUBLIC")
    now = [1000.0]
    env.svc.clock = lambda: now[0]
    env.backend.mode = "unavailable"
    for _ in range(2):                                                    # the SAME document trips its breaker at 2
        with pytest.raises(env.mods.errors.BackendUnavailable):
            env.svc.convert("acme", "p.docx", "pdf")
    with pytest.raises(env.mods.errors.BackendUnavailable, match="circuit"):
        env.svc.convert("acme", "p.docx", "pdf")
    assert env.backend.calls.count("convert") == 2                       # not even attempted
    now[0] += 31                                                          # back-off elapsed: one probe goes through
    env.backend.mode = None
    assert env.svc.convert("acme", "p.docx", "pdf")["verified"]


def test_per_tenant_concurrency_limit(env, kit):
    import threading
    env.put("acme", "p.docx", kit.make_docx(["x"]), label="PUBLIC")
    env.svc.max_concurrent_per_tenant = 1
    gate, entered = threading.Event(), threading.Event()
    real = env.backend._do_convert

    def slow(ref, fmt):
        entered.set()
        gate.wait(5)
        return real(ref, fmt)
    env.backend._do_convert = slow
    t = threading.Thread(target=lambda: env.svc.convert("acme", "p.docx", "pdf"))
    t.start()
    assert entered.wait(5)
    with pytest.raises(env.mods.errors.BackendUnavailable, match="concurrent"):
        env.svc.export("acme", "p.docx", "pdf")
    gate.set()
    t.join()


# ── 4. trust zone enforced by the RESOLVED address ─────────────────────────────────────────────────────────
def test_private_host_resolving_to_a_public_ip_is_refused(mods):
    S = mods.safe_fetch
    pol = S.FetchPolicy.of([S.HostRule("editor.test", S.ZONE_PRIVATE, frozenset({80}))])
    with pytest.raises(S.SSRFBlocked, match="PRIVATE"):
        S.safe_fetch("http://editor.test/x", policy=pol, resolver=lambda h, p: ["93.184.216.34"])


def test_local_host_must_resolve_to_loopback(mods):
    S = mods.safe_fetch
    pol = S.FetchPolicy.of([S.HostRule("editor.test", S.ZONE_LOCAL, frozenset({80}))])
    for ip in ("10.0.0.5", "93.184.216.34", "192.168.1.1"):
        with pytest.raises(S.SSRFBlocked):
            S.safe_fetch("http://editor.test/x", policy=pol, resolver=lambda h, p, ip=ip: [ip])
    assert S.ip_block_reason("127.0.0.1", "LOCAL") is None and S.ip_block_reason("::1", "LOCAL") is None


def test_regression_guard_public_host_resolving_private_follows_the_ssrf_rules(mods):
    """REGRESSION GUARD ONLY: passed before the review fixes too."""
    S = mods.safe_fetch
    pol = S.FetchPolicy.of([S.HostRule("editor.test", S.ZONE_PUBLIC, frozenset({443}))])
    for ip in ("10.0.0.5", "127.0.0.1", "169.254.169.254", "::ffff:10.0.0.1"):
        with pytest.raises(S.SSRFBlocked):
            S.safe_fetch("https://editor.test/x", policy=pol, resolver=lambda h, p, ip=ip: [ip])


def test_local_zone_maps_from_backend_zone(mods):
    cfg = mods.onlyoffice.OnlyOfficeConfig(url="http://editor.test:8080", jwt_secret="x", zone="LOCAL")
    assert cfg.fetch_policy().hosts["editor.test"].zone == "LOCAL"
    assert mods.onlyoffice.OnlyOfficeConfig(url="https://e.test", jwt_secret="x", zone="EXTERNAL").fetch_policy().hosts["e.test"].zone == "PUBLIC"


# ── 5. total deadline enforced at the socket ───────────────────────────────────────────────────────────────────
def test_slow_drip_server_is_aborted_within_the_deadline(mods):
    import socket, threading
    S = mods.safe_fetch
    ls = socket.socket()
    ls.bind(("127.0.0.1", 0))
    ls.listen(1)
    port = ls.getsockname()[1]
    stop = threading.Event()

    def drip():
        c, _ = ls.accept()
        c.recv(65536)
        try:
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n")
            while not stop.is_set():
                c.sendall(b"x")
                time.sleep(0.5)
        except OSError:
            pass
        finally:
            c.close()
    threading.Thread(target=drip, daemon=True).start()
    pol = S.FetchPolicy.of([S.HostRule("editor.test", S.ZONE_LOCAL, frozenset({port}))], timeout_s=1.5)
    t0 = time.monotonic()
    with pytest.raises(S.FetchUnavailable, match="deadline"):
        S.safe_fetch(f"http://editor.test:{port}/x", policy=pol, resolver=lambda h, p: ["127.0.0.1"])
    stop.set()
    ls.close()
    assert time.monotonic() - t0 < 4


def test_slow_drip_headers_are_aborted_too(mods):
    import socket, threading
    S = mods.safe_fetch
    ls = socket.socket(); ls.bind(("127.0.0.1", 0)); ls.listen(1); port = ls.getsockname()[1]

    def drip():
        c, _ = ls.accept()
        c.recv(65536)
        try:
            for ch in b"HTTP/1.1 200 OK\r\nX-A: " + b"a" * 100:
                c.sendall(bytes([ch])); time.sleep(0.4)
        except OSError:
            pass
        finally:
            c.close()
    threading.Thread(target=drip, daemon=True).start()
    pol = S.FetchPolicy.of([S.HostRule("editor.test", S.ZONE_LOCAL, frozenset({port}))], timeout_s=1.5)
    t0 = time.monotonic()
    with pytest.raises(S.FetchUnavailable):
        S.safe_fetch(f"http://editor.test:{port}/x", policy=pol, resolver=lambda h, p: ["127.0.0.1"])
    ls.close()
    assert time.monotonic() - t0 < 4


# ── 6. unimplemented providers are not "reachable" ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("prov", ["collabora", "elixiroffice"])
def test_connect_check_reports_unimplemented_providers_as_unavailable(env, mods, prov):
    env.svc.backend = mods.backend.UnimplementedBackend(mods.backend.BackendDecl.parse({"provider": prov}))
    r = env.svc.connect_check("acme")
    assert r["backend_reachable"] is False and r["status"] in ("denied", "unavailable")
    assert "not implemented" in r["detail"] and not any(r["capabilities"].values())


# ── 7. DTD scan covers the whole part ──────────────────────────────────────────────────────────────────────────
def test_dtd_after_a_large_padding_comment_is_still_refused(mods):
    O = mods.ooxml
    pad = b"<!--" + b"a" * 200000 + b"-->"
    raw = b'<?xml version="1.0"?>' + pad + b'<!DOCTYPE x [<!ENTITY a "b">]><root/>'
    with pytest.raises(mods.errors.InvalidInput, match="DTD"):
        O._parse(raw)
    u16 = ('<?xml version="1.0" encoding="UTF-16"?>' + "<!--" + "a" * 100000 + '--><!DOCTYPE x [<!ENTITY a "b">]><root/>').encode("utf-16")
    with pytest.raises(mods.errors.InvalidInput, match="DTD"):
        O._parse(u16)
    O._parse(b'<?xml version="1.0"?><root>fine</root>')


# ── 8. out-of-scope parts must be unchanged ───────────────────────────────────────────────────────────────────
def _with_part(data: bytes, name: str, content: str) -> bytes:
    zin = zipfile.ZipFile(io.BytesIO(data))
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for n in zin.namelist():
            z.writestr(n, zin.read(n))
        z.writestr(name, content)
    return b.getvalue()


@pytest.mark.parametrize("name,content", [
    ("word/footer1.xml", '<w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>INJECTED FOOTER</w:t></w:r></w:p></w:ftr>'),
    ("word/_rels/document.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="r9" Type="x" Target="http://evil.example/track" TargetMode="External"/></Relationships>'),
    ("word/comments.xml", '<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:comment><w:p><w:r><w:t>sneaky</w:t></w:r></w:p></w:comment></w:comments>'),
])
def test_doctored_editor_result_with_extra_parts_is_rejected_and_preserved(env, kit, name, content):
    env.put("acme", "a.docx", kit.make_docx(["Price 100 EUR"]), label="PUBLIC")
    p = env.svc.propose_patch("acme", "a.docx", [{"type": "replace_text", "search": "100 EUR", "replace": "1 EUR"}])
    real = env.backend._do_apply_edit

    def doctored(ref, ops, **k):
        out = real(ref, ops, **k)
        data = _with_part(out.content, name, content)
        return env.mods.backend.BackendFile(content=data, filename="result.docx", sha256=hashlib.sha256(data).hexdigest())
    env.backend._do_apply_edit = doctored
    with pytest.raises(env.mods.errors.VerificationFailed) as ei:
        env.svc.apply_patch("acme", proposal_id=p["proposal_id"], base_hash=p["base_hash"], backend=p["backend"],
                            patch_digest=p["patch_digest"], expected_version=1, idempotency_key="idem-doctor-0001",
                            approval=APPROVAL)
    assert env.store.head("acme", "a.docx").version == 1 and ei.value.extra["draft"]["doc_id"].startswith("drafts/")


# ── 10. labels, policy key length, bearer transport ───────────────────────────────────────────────────────────────
def test_set_label_needs_approval_and_flags_downgrades(env, kit):
    env.put("acme", "l.docx", kit.make_docx(["x"]), label="RESTRICTED")
    staged = env.svc.__class__  # noqa: F841
    r = env.svc.set_label("acme", "l.docx", "internal", approval=APPROVAL, actor="alice")
    assert r["label"] == "INTERNAL" and r["previous_label"] == "RESTRICTED" and r["downgrade"] is True and r["verified"]
    rec = env.store.receipt_log("acme").find(kind="set_label")[0]
    assert rec["approval_ref"]["marker"]["by"] == "alice" and rec["downgrade"] is True
    up = env.svc.set_label("acme", "l.docx", "CONFIDENTIAL", approval=APPROVAL)
    assert up["downgrade"] is False
    with pytest.raises(env.mods.errors.InvalidInput):
        env.svc.set_label("acme", "l.docx", "TOP-SECRET", approval=APPROVAL)


def test_set_label_capability_stages_without_a_verified_approval(env, kit):
    op = env.mods.operator.build_document_editor_operator(lambda: env.svc)
    env.put("acme", "l.docx", kit.make_docx(["x"]), label="RESTRICTED")
    r = op._handlers["document.set_label"]({"tenant": "acme", "doc_id": "l.docx", "label": "PUBLIC"})
    assert r["status"] == "pending_approval" and env.store.get_classification("acme", "l.docx") == "RESTRICTED"
    r = op._handlers["document.set_label"]({"tenant": "acme", "doc_id": "l.docx", "label": "PUBLIC", "_approval": APPROVAL})
    assert r["status"] == "ok" and env.store.get_classification("acme", "l.docx") == "PUBLIC"


def test_short_policy_signing_keys_are_refused(mods):
    C = mods.classification
    short = "k" * 31
    payload = {"tenant": "acme", "expires": time.time() + 3600, "matrix": {"INTERNAL": {"zones": ["EXTERNAL"]}}}
    sp = {"payload": payload, "signature": C.sign_policy(payload, short)}
    assert C.verify_policy(payload, sp["signature"], short, tenant="acme") is False
    with pytest.raises(mods.errors.ConfigError, match="32"):
        C.TenantPolicy.build("acme", {"onlyoffice": "EXTERNAL"}, signed_policy=sp, signing_key=short)
    ok = "k" * 32
    sp = {"payload": payload, "signature": C.sign_policy(payload, ok)}
    assert C.TenantPolicy.build("acme", {"onlyoffice": "EXTERNAL"}, signed_policy=sp, signing_key=ok).signed


def test_bearer_transport_adds_the_authorization_header(mods, server):
    import importlib
    from agentic_os.mission.operators import HTTPOperatorClient
    tr = importlib.import_module("document-editor.transport")
    srv = server(lambda r: (200, {"Content-Type": "application/json"}, b'{"result": {"status": "ok"}}'))
    c = HTTPOperatorClient({"document-editor": f"http://127.0.0.1:{srv.port}"}, transport=tr.bearer_transport("tok-123"))
    assert c.invoke("document-editor", "document.connect_check", {"tenant": "acme"}, "idem")["status"] == "ok"
    assert srv.requests[0].headers["Authorization"] == "Bearer tok-123"
    with pytest.raises(ValueError):
        tr.bearer_transport("")


# ── delta review: breaker isolation, package risks, label reset, transport ───────────────────────────────────
def test_one_tenants_failures_never_block_another_tenant(env, kit):
    """FAILS on the previous code: the breaker was service-global."""
    for i in range(3):
        env.put("acme", f"d{i}.docx", kit.make_docx([f"doc {i}"]), label="PUBLIC")
    env.put("globex", "g.docx", kit.make_docx(["globex"]), label="PUBLIC")
    env.backend.mode = "unavailable"
    for i in range(3):                                           # acme trips its TENANT breaker with 3 different docs
        with pytest.raises(env.mods.errors.BackendUnavailable):
            env.svc.convert("acme", f"d{i}.docx", "pdf")
    with pytest.raises(env.mods.errors.BackendUnavailable, match="circuit"):
        env.svc.convert("acme", "d0.docx", "pdf")
    env.backend.mode = None
    assert env.svc.convert("globex", "g.docx", "pdf")["verified"]     # globex is unaffected
    with pytest.raises(env.mods.errors.BackendUnavailable, match="circuit"):
        env.svc.convert("acme", "d1.docx", "pdf")                    # acme still backed off


def test_a_malformed_document_only_blocks_itself(env, kit):
    env.put("acme", "bad.docx", kit.make_docx(["bad"]), label="PUBLIC")
    env.put("acme", "ok.docx", kit.make_docx(["ok"]), label="PUBLIC")
    env.backend.mode = "unavailable"
    for _ in range(2):
        with pytest.raises(env.mods.errors.BackendUnavailable):
            env.svc.convert("acme", "bad.docx", "pdf")
    env.backend.mode = None
    assert env.svc.convert("acme", "ok.docx", "pdf")["verified"]
    with pytest.raises(env.mods.errors.BackendUnavailable, match="circuit"):
        env.svc.convert("acme", "bad.docx", "pdf")


def _rewrite(data: bytes, doc_xml_edit=None, add: dict = None, ct_add: str = "") -> bytes:
    zin = zipfile.ZipFile(io.BytesIO(data))
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for n in zin.namelist():
            c = zin.read(n)
            if n == "word/document.xml" and doc_xml_edit:
                c = doc_xml_edit(c.decode()).encode()
            if n == "[Content_Types].xml" and ct_add:
                c = c.decode().replace("</Types>", ct_add + "</Types>").encode()
            z.writestr(n, c)
        for n, c in (add or {}).items():
            z.writestr(n, c)
    return b.getvalue()


W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
ATTACKS = {
    "dde-instrText": dict(doc_xml_edit=lambda x: x.replace("</w:body>", '<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText>DDEAUTO c:\\\\windows\\\\system32\\\\cmd.exe "/k calc"</w:instrText></w:r></w:p></w:body>')),
    "fldSimple": dict(doc_xml_edit=lambda x: x.replace("</w:body>", '<w:p><w:fldSimple w:instr="INCLUDETEXT \'file:///etc/passwd\'"/></w:p></w:body>')),
    "embedded-ole": dict(add={"word/embeddings/evil.bin": b"MZ\x90\x00"}),
    "vba-macro": dict(add={"word/vbaProject.bin": b"\xd0\xcf\x11"},
                      ct_add='<Override PartName="/word/vbaProject.bin" ContentType="application/vnd.ms-office.vbaProject"/>'),
    "activex": dict(add={"word/activeX/activeX1.xml": "<ax/>"}),
    "altChunk": dict(doc_xml_edit=lambda x: x.replace("</w:body>", '<w:altChunk r:id="rId9" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/></w:body>')),
    "external-rel": dict(  # (already caught before; guard)
        add={"word/_rels/document.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="r9" Type="t" Target="http://evil.example/x" TargetMode="External"/></Relationships>'}),
}


@pytest.mark.parametrize("attack", sorted(ATTACKS))
def test_doctored_editor_package_is_rejected_for_every_active_content_class(env, kit, attack):
    """FAILS on the previous code for everything except external-rel: text compare ignored fields and extra parts."""
    env.put("acme", "a.docx", kit.make_docx(["Price 100 EUR"]), label="PUBLIC")
    p = env.svc.propose_patch("acme", "a.docx", [{"type": "replace_text", "search": "100 EUR", "replace": "1 EUR"}])
    real = env.backend._do_apply_edit

    def doctored(ref, ops, **k):
        out = real(ref, ops, **k)
        data = _rewrite(out.content, **ATTACKS[attack])
        return env.mods.backend.BackendFile(content=data, filename="result.docx", sha256=hashlib.sha256(data).hexdigest())
    env.backend._do_apply_edit = doctored
    with pytest.raises(env.mods.errors.VerificationFailed):
        env.svc.apply_patch("acme", proposal_id=p["proposal_id"], base_hash=p["base_hash"], backend=p["backend"],
                            patch_digest=p["patch_digest"], expected_version=1, idempotency_key=f"idem-{attack}-001",
                            approval=APPROVAL)
    assert env.store.head("acme", "a.docx").version == 1


def test_clean_edit_with_preexisting_fields_and_links_is_still_accepted(env, kit):
    """REGRESSION GUARD (passed before too): the stricter package checks must not reject legitimate edits."""
    base = _rewrite(kit.make_docx(["Price 100 EUR"]), doc_xml_edit=lambda x: x.replace(
        "</w:body>", '<w:p><w:fldSimple w:instr="PAGE"/></w:p></w:body>'))
    env.put("acme", "f.docx", base, label="PUBLIC")
    p = env.svc.propose_patch("acme", "f.docx", [{"type": "replace_text", "search": "100 EUR", "replace": "1 EUR"}])
    r = env.svc.apply_patch("acme", proposal_id=p["proposal_id"], base_hash=p["base_hash"], backend=p["backend"],
                            patch_digest=p["patch_digest"], expected_version=1, idempotency_key="idem-fields-ok-01",
                            approval=APPROVAL)
    assert r["status"] == "ok"


@pytest.mark.parametrize("old,expect", [("PUBLIC", None), ("INTERNAL", None), ("CONFIDENTIAL", "CONFIDENTIAL"),
                                        ("RESTRICTED", "RESTRICTED")])
def test_a_replaced_file_never_inherits_a_looser_label(env, kit, old, expect):
    """FAILS on the previous code for PUBLIC/INTERNAL: the replacement kept the old label."""
    env.put("acme", "v.docx", kit.make_docx(["victim"]), label=old)
    env.put("acme", "v.docx", kit.make_docx(["completely different secret content"]))     # replaced in place
    head = env.store.head("acme", "v.docx")
    assert head.version == 2 and env.store.get_classification("acme", "v.docx") == expect
    eff = env.mods.classification.effective_label(env.store.get_classification("acme", "v.docx"))
    assert eff in ("CONFIDENTIAL", "RESTRICTED")
    if old in ("PUBLIC", "INTERNAL"):                   # blocked until a human re-labels it
        with pytest.raises(env.mods.errors.PolicyDenied):
            env.svc.convert("acme", "v.docx", "pdf")
        env.svc.set_label("acme", "v.docx", old, approval=APPROVAL)
        assert env.svc.convert("acme", "v.docx", "pdf")["verified"]


def test_unchanged_file_keeps_its_label(env, kit):
    """REGRESSION GUARD (passed before too): only a CHANGED file loses a looser label."""
    env.put("acme", "k.docx", kit.make_docx(["same"]), label="PUBLIC")
    env.store.head("acme", "k.docx")
    assert env.store.get_classification("acme", "k.docx") == "PUBLIC"


@pytest.mark.parametrize("a,b", [("Stra\u00dfe.docx", "Strasse.docx"), ("\ufb01le.docx", "file.docx"),
                                 ("\u212a.docx", "k.docx"), ("\u03a3.docx", "\u03c2.docx")])
def test_casefold_collisions_fail_closed_and_share_identity(mods, a, b):
    """REGRESSION GUARD (passed before too): colliding names are one document identity."""
    assert mods.store.canonical_id(a) == mods.store.canonical_id(b)
    assert mods.store.LocalFSStore._key(a) == mods.store.LocalFSStore._key(b)


def test_ambiguous_collision_locks_out_rather_than_leaking(env, kit):
    """REGRESSION GUARD (passed before too) documenting the known author lock-out limitation."""
    env.put("acme", "Stra\u00dfe.docx", kit.make_docx(["victim"]), label="PUBLIC")
    env.put("acme", "Strasse.docx", kit.make_docx(["attacker"]))
    if env.mods.store.canonical_id("Stra\u00dfe.docx") != env.mods.store.canonical_id("Strasse.docx"):
        pytest.skip("this casefold does not collide")
    if len([n for n in os.listdir(env.root / "acme") if n.endswith(".docx")]) < 2:
        pytest.skip("case-insensitive FS with full case folding (APFS): the two names are ONE file here")
    with pytest.raises(env.mods.errors.InvalidPath, match="ambiguous"):
        env.store.head("acme", "Strasse.docx")


def test_bearer_transport_refuses_plain_http_to_non_loopback(mods):
    import importlib
    tr = importlib.import_module("document-editor.transport").bearer_transport("tok")
    with pytest.raises(ValueError, match="https"):
        tr("http://docedit.internal:8216/invoke", {}, {}, 1)
    with pytest.raises(ValueError, match="https"):
        tr("http://10.0.0.5/invoke", {}, {}, 1)
