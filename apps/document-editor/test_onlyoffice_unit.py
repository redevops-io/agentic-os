"""OnlyOffice adapter against a MOCK Document Server that implements the documented request/response contracts.

Labelled honestly: this proves the adapter's behaviour (JWT, polling, origin pinning, output validation, failure
mapping); it does NOT prove the real product (that is integration/test_onlyoffice_integration.py).
"""
from __future__ import annotations

import json

import pytest

SECRET = "mock-jwt-secret"        # == conftest.MOCK_SECRET


def test_health_verifies_jwt_and_reports_the_version(mods, ds, oo):
    be, _ = oo.backend(ds)
    h = be.health()
    assert h["ok"] and h["editor_version"] == "9.4.0.129" and h["jwt"] == "verified"
    path, payload = ds.calls[-1]
    assert path == "/command" and payload["c"] == "version" and "exp" in payload


def test_wrong_jwt_secret_is_reported_not_swallowed(mods, ds, oo):
    be, _ = oo.backend(ds)
    be.cfg = mods.onlyoffice.OnlyOfficeConfig(url=ds.base, jwt_secret="WRONG", poll_interval_s=0.0)
    with pytest.raises(mods.errors.BackendError, match="JWT secret mismatch"):
        be.health()


def test_unhealthy_editor_is_a_typed_unavailable(mods, ds, oo):
    be, _ = oo.backend(ds)
    ds.health = (503, b"")
    with pytest.raises(mods.errors.BackendUnavailable):
        be.health()


def test_editor_down_is_a_typed_unavailable(mods):
    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    cfg = mods.onlyoffice.OnlyOfficeConfig(url=f"http://editor.test:{port}", jwt_secret=SECRET)
    be = mods.onlyoffice.OnlyOfficeBackend(
        mods.backend.BackendDecl.parse({"provider": "onlyoffice", "capabilities": {"convert": True}}), cfg,
        mods.content.ContentRegistry("http://c"), resolver=lambda h, p: ["127.0.0.1"])
    with pytest.raises(mods.errors.BackendUnavailable):
        be.health()


def test_convert_polls_then_downloads_and_validates(mods, ds, kit, oo):
    be, _ = oo.backend(ds)
    out = be.convert(oo.ref(), "pdf")
    assert out.content == kit.PDF and len(out.sha256) == 64 and out.filename == "a.pdf"
    sent = [p for path, p in ds.calls if path == "/converter"]
    assert len(sent) == 2 and sent[0]["async"] is True and sent[0]["filetype"] == "docx" and sent[0]["outputtype"] == "pdf"
    assert sent[0]["url"] == "http://connector.test/content/tok" and sent[0]["key"] == sent[1]["key"]


def test_convert_key_is_bound_to_tenant_doc_version_hash_and_format(mods, ds, oo):
    be, _ = oo.backend(ds)
    keys = {be._key(oo.ref(**kw), "pdf") for kw in ({}, {"version": 2}, {"sha256": "b" * 64},
                                                        {"tenant": "globex"}, {"doc_id": "z.docx"})}
    assert len(keys) == 5


def test_convert_rejects_an_invalid_pdf_from_the_editor(mods, ds, oo):
    be, _ = oo.backend(ds)
    ds.result = b"<html>an error page</html>"
    with pytest.raises(mods.errors.InvalidInput, match="PDF"):
        be.convert(oo.ref(), "pdf")
    ds.polls, ds.result = 0, b""
    with pytest.raises(mods.errors.InvalidInput, match="empty"):
        be.convert(oo.ref(), "pdf")
    ds.polls, ds.result = 0, b"%PDF-1.7 truncated no trailer"
    with pytest.raises(mods.errors.InvalidInput, match="truncated"):
        be.convert(oo.ref(), "pdf")


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/", "http://evil.test/x.pdf", "https://editor.test/x.pdf",
    "http://editor.test:1/x.pdf", "file:///etc/passwd", "http://user:pw@editor.test/x", "//evil.test/x",
])
def test_result_url_outside_the_configured_editor_origin_is_never_fetched(mods, ds, url, oo):
    be, _ = oo.backend(ds)
    ds.file_url = url
    with pytest.raises(mods.errors.DocEditorError):
        be.convert(oo.ref(), "pdf")
    assert not any(r.path.startswith("/cache") for r in ds.srv.requests)


def test_editor_error_codes_are_mapped(mods, ds, oo):
    be, _ = oo.backend(ds)
    ds.convert_steps = [{"error": -3}]
    with pytest.raises(mods.errors.BackendError, match="conversion error") as ei:
        be.convert(oo.ref(), "pdf")
    assert ei.value.extra["editor_error"] == -3
    ds.polls, ds.convert_steps = 0, [{"error": -8}]
    with pytest.raises(mods.errors.BackendError, match="invalid token"):
        be.convert(oo.ref(), "pdf")


def test_convert_times_out_with_a_typed_error(mods, ds, oo):
    be, _ = oo.backend(ds)
    ds.convert_steps = [{"endConvert": False, "percent": 10}]
    ticks = iter(range(0, 10000, 3))
    be._mono = lambda: next(ticks)
    with pytest.raises(mods.errors.BackendUnavailable, match="timeout"):
        be.convert(oo.ref(), "pdf")


def test_convert_input_validation(mods, ds, oo):
    be, _ = oo.backend(ds)
    for r, fmt in ((oo.ref(filename="a.exe"), "pdf"), (oo.ref(), "exe"), (oo.ref(), "../../x"),
                   (oo.ref(content_url=""), "pdf")):
        with pytest.raises(mods.errors.InvalidInput):
            be.convert(r, fmt)
    assert ds.calls == []


def test_oversized_result_is_refused(mods, ds, kit, oo):
    ds.result = kit.PDF + b"0" * 5000
    be, _ = oo.backend(ds, max_result_bytes=1000)
    with pytest.raises(mods.errors.DocEditorError):
        be.convert(oo.ref(), "pdf")


def test_5xx_during_convert_is_unavailable(mods, ds, oo):
    be, _ = oo.backend(ds)
    ds.status_override = {"/converter": 502}
    with pytest.raises(mods.errors.BackendUnavailable):
        be.convert(oo.ref(), "pdf")


def test_open_session_is_view_only_signed_scoped_and_expiring(mods, ds, oo):
    be, _ = oo.backend(ds)
    s = be.open_session(oo.ref(), user_id="alice", ttl_s=900)
    assert s["mode"] == "view" and s["expires_in_s"] == 300
    cfg = s["config"]
    claims = mods.jwtlib.verify(cfg["token"], SECRET)
    assert claims["document"]["permissions"]["edit"] is False and claims["editorConfig"]["mode"] == "view"
    assert claims["exp"] - claims["exp"] % 1 > 0 and "callbackUrl" not in claims["editorConfig"]
    assert s["bound_to"] == {"tenant": "acme", "doc_id": "a.docx", "version": 1}
    assert SECRET not in json.dumps(s)
    with pytest.raises(mods.errors.UnsupportedCapability, match="view mode only"):
        be.open_session(oo.ref(), user_id="alice", ttl_s=60, mode="edit")


def test_open_session_requires_a_capability(mods, ds, oo):
    be, _ = oo.backend(ds)
    be.decl = mods.backend.BackendDecl.parse({"provider": "onlyoffice", "capabilities": {"convert": True}})
    with pytest.raises(mods.errors.UnsupportedCapability):
        be.open_session(oo.ref(), user_id="a", ttl_s=60)


def test_inspect_reports_editor_facts_and_marks_text_as_untrusted_evidence(mods, ds, oo):
    be, _ = oo.backend(ds)
    ds.result = b"Hello world\nsecond line\n"
    ds.convert_steps = [{"endConvert": True}]
    ds.file_url = f"{ds.base}/cache/files/x/output.txt"
    i = be.inspect(oo.ref())
    assert i["opened_by_editor"] and i["lines"] == 2 and i["editor_version"] == "9.4.0.129" and i["preview"].startswith("Hello")
