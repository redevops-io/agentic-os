"""ONLYOFFICE adapter configuration (credentials only from the environment) and strict HS256 JWT."""
from __future__ import annotations

import pytest


def test_onlyoffice_credentials_only_from_the_environment(mods):
    O = mods.onlyoffice
    with pytest.raises(mods.errors.ConfigError):
        O.OnlyOfficeConfig.from_env(env={})
    with pytest.raises(mods.errors.ConfigError):
        O.OnlyOfficeConfig.from_env(env={"DOCEDITOR_ONLYOFFICE_URL": "http://x"})
    cfg = O.OnlyOfficeConfig.from_env(env={"DOCEDITOR_ONLYOFFICE_URL": "https://oo.example/",
                                            "DOCEDITOR_ONLYOFFICE_JWT_SECRET": "s3cret-value"})
    assert "s3cret-value" not in repr(cfg) and "s3cret-value" not in str(cfg)
    assert cfg.url == "https://oo.example"


@pytest.mark.parametrize("url", ["https://u:p@oo.example", "https://oo.example/path", "https://oo.example/?x=1",
                                 "ftp://oo.example", "https://oo.example#f"])
def test_editor_url_must_be_a_bare_origin(mods, url):
    cfg = mods.onlyoffice.OnlyOfficeConfig(url=url, jwt_secret="x")
    with pytest.raises(mods.errors.DocEditorError):
        cfg.origin()


# ── JWT ──────────────────────────────────────────────────────────────────────────────────────────────────────
def test_jwt_roundtrip_and_strictness(mods):
    J = mods.jwtlib
    t = J.sign({"a": 1}, "k", ttl_s=60)
    assert J.verify(t, "k")["a"] == 1
    with pytest.raises(J.JWTError):
        J.verify(t, "other")
    with pytest.raises(J.JWTError, match="expired"):
        J.verify(J.sign({"a": 1}, "k", ttl_s=-5), "k")
    h, p, s = t.split(".")
    import base64
    none_hdr = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode()
    for bad in (f"{none_hdr}.{p}.", f"{none_hdr}.{p}.{s}", "a.b", "x", f"{h}.{p}.AAAA", f"{h}.{p}x.{s}"):
        with pytest.raises(J.JWTError):
            J.verify(bad, "k")
    hs512 = base64.urlsafe_b64encode(b'{"alg":"HS512","typ":"JWT"}').rstrip(b"=").decode()
    with pytest.raises(J.JWTError):
        J.verify(f"{hs512}.{p}.{s}", "k")
    with pytest.raises(J.JWTError):
        J.sign({}, "")


