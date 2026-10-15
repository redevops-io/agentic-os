"""Document Builder edit path against the MOCK Document Server (fixed template, data as JSON, one write, no retry)."""
from __future__ import annotations

import pytest

SECRET = "mock-jwt-secret"        # == conftest.MOCK_SECRET


# ── builder (write) ───────────────────────────────────────────────────────────────────────────────────────
def test_apply_edit_uses_the_fixed_template_and_passes_ops_as_data(mods, ds, kit, oo):
    be, content = oo.backend(ds)
    ds.result = kit.make_docx(["Edited"])
    ops = [{"type": "replace_text", "search": "'; process.exit(); //", "replace": "x", "match_case": True}]
    out = be.apply_edit(oo.ref(), ops, track_changes=True, idem_key="k-123456")
    assert out.content == ds.result
    first = [p for path, p in ds.calls if path == "/docbuilder"][0]
    assert first["argument"] == {"src": "http://connector.test/content/tok", "ops": ops, "track": True}
    assert first["url"].startswith("http://connector.test/content/")        # the script is fetched from a signed URL
    token = first["url"].rsplit("/", 1)[1]
    claims = content.verify(token)
    assert claims["k"] == "script"
    script = content.inline_bytes(claims["h"])
    assert script == mods.patch.builder_script("docx") and b"process.exit" not in script   # payload never in source


def test_apply_edit_write_failure_after_send_is_outcome_unknown_and_not_retried(mods, ds, oo):
    be, _ = oo.backend(ds)
    ds.status_override = {"/docbuilder": 500}
    with pytest.raises(mods.errors.OutcomeUnknown):
        be.apply_edit(oo.ref(), [{"type": "append_paragraph", "text": "x"}], track_changes=False, idem_key="k-123456")
    assert len([r for r in ds.srv.requests if r.path == "/docbuilder"]) == 1          # exactly one attempt


def test_apply_edit_connection_reset_after_send_is_outcome_unknown(mods, server, kit, oo):
    import socket, threading
    ls = socket.socket(); ls.bind(("127.0.0.1", 0)); ls.listen(1); port = ls.getsockname()[1]

    def rude():
        c, _ = ls.accept()
        c.recv(65536)
        c.close()                       # request received, then the connection dies without a response
    threading.Thread(target=rude, daemon=True).start()
    cfg = mods.onlyoffice.OnlyOfficeConfig(url=f"http://editor.test:{port}", jwt_secret=SECRET, poll_interval_s=0)
    decl = mods.backend.BackendDecl.parse({"provider": "onlyoffice", "capabilities": {"automated_edit": True}})
    be = mods.onlyoffice_edit.OnlyOfficeEditBackend(decl, cfg, mods.content.ContentRegistry("http://c", secret="s"),
                                                    resolver=lambda h, p: ["127.0.0.1"], sleep=lambda s: None)
    with pytest.raises(mods.errors.OutcomeUnknown):
        be.apply_edit(oo.ref(), [{"type": "append_paragraph", "text": "x"}], track_changes=False, idem_key="k-123456")
    ls.close()


def test_apply_edit_connection_refused_is_unavailable_not_unknown(mods, oo):
    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    cfg = mods.onlyoffice.OnlyOfficeConfig(url=f"http://editor.test:{port}", jwt_secret=SECRET)
    decl = mods.backend.BackendDecl.parse({"provider": "onlyoffice", "capabilities": {"automated_edit": True}})
    be = mods.onlyoffice_edit.OnlyOfficeEditBackend(decl, cfg, mods.content.ContentRegistry("http://c", secret="s"),
                                                    resolver=lambda h, p: ["127.0.0.1"], sleep=lambda s: None)
    with pytest.raises(mods.errors.BackendUnavailable) as ei:
        be.apply_edit(oo.ref(), [{"type": "append_paragraph", "text": "x"}], track_changes=False, idem_key="k-123456")
    assert not isinstance(ei.value, mods.errors.OutcomeUnknown)


def test_apply_edit_result_must_be_a_valid_docx_from_the_editor_origin(mods, ds, kit, oo):
    be, _ = oo.backend(ds)
    ds.result = b"garbage"
    with pytest.raises(mods.errors.InvalidInput):
        be.apply_edit(oo.ref(), [{"type": "append_paragraph", "text": "x"}], track_changes=False, idem_key="k-123456")
    ds.builder_urls = {"result.docx": "http://169.254.169.254/x"}
    with pytest.raises(mods.errors.BackendError, match="origin"):
        be.apply_edit(oo.ref(), [{"type": "append_paragraph", "text": "x"}], track_changes=False, idem_key="k-123456")
