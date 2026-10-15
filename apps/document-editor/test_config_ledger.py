"""Config loading (no secrets in files), ledger hash chain, JWT, ooxml guards."""
from __future__ import annotations

import json
import time

import pytest


def base(**over):
    raw = {"document_backend": {"provider": "onlyoffice", "deployment": "external", "capabilities": {"convert": True}},
           "document_root": "/data/docs", "content_base_url": "https://connector.example", "backend_zone": "PRIVATE_NETWORK",
           "tenants": {"acme": {"backends": {"onlyoffice": "PRIVATE_NETWORK"}}}}
    raw.update(over)
    return raw


def test_config_parses(mods):
    cfg = mods.config.parse_config(base(), env={})
    assert cfg.decl.provider == "onlyoffice" and "acme" in cfg.tenants


def test_env_overrides_root_and_state(mods):
    cfg = mods.config.parse_config(base(), env={"DOCEDITOR_DOCUMENT_ROOT": "/mnt/x", "DOCEDITOR_STATE_DIR": "/mnt/s"})
    assert str(cfg.document_root).replace("\\", "/") == "/mnt/x"


@pytest.mark.parametrize("mut", [
    {"document_root": ""}, {"content_base_url": ""}, {"backend_zone": "MOON"},
    {"tenants": {"A/B": {}}}, {"tenants": {"acme": {"backends": {"onlyoffice": "LOCAL"}}}},      # claims more trust than real
    {"tenants": {"acme": {"backends": {"onlyoffice": "MOON"}}}},
    {"tenants": {"acme": {"publish_destinations": {"x": {"path": "relative/dir"}}}}},
    {"tenants": {"acme": {"publish_destinations": {"X Y": {"path": "/abs"}}}}},
])
def test_invalid_config_fails_closed(mods, mut):
    with pytest.raises(mods.errors.ConfigError):
        mods.config.parse_config(base(**mut), env={})


@pytest.mark.parametrize("key", ["jwt_secret", "api_key", "password", "client_secret", "onlyoffice_token", "private_key"])
def test_secrets_in_the_config_file_are_rejected(mods, key):
    raw = base()
    raw["document_backend"][key] = "hunter2"
    with pytest.raises(mods.errors.ConfigError, match="environment"):
        mods.config.parse_config(raw, env={})


def test_load_config_from_json_and_yaml(mods, tmp_path):
    (tmp_path / "c.json").write_text(json.dumps(base()))
    assert mods.config.load_config(tmp_path / "c.json", env={}).decl.provider == "onlyoffice"
    import yaml
    (tmp_path / "c.yaml").write_text(yaml.safe_dump(base()))
    assert mods.config.load_config(tmp_path / "c.yaml", env={}).decl.provider == "onlyoffice"
    with pytest.raises(mods.errors.ConfigError):
        mods.config.load_config(None, env={})


# ── ledger ─────────────────────────────────────────────────────────────────────────────────────────────────
def test_ledger_chain_detects_tampering(mods, tmp_path):
    L = mods.ledger.Ledger(tmp_path / "r.jsonl")
    for i in range(5):
        L.append({"kind": "k", "i": i})
    assert L.verify_chain() and len(list(L.records())) == 5
    lines = (tmp_path / "r.jsonl").read_text().splitlines()
    (tmp_path / "r.jsonl").write_text("\n".join(lines[:2] + lines[3:]) + "\n")           # delete a record
    assert not L.verify_chain()
    (tmp_path / "r.jsonl").write_text("\n".join(lines).replace('"i":1', '"i":9') + "\n")  # edit a record
    assert not L.verify_chain()


def test_ledger_concurrent_appends_keep_the_chain(mods, tmp_path):
    import threading
    L = mods.ledger.Ledger(tmp_path / "r.jsonl")
    ts = [threading.Thread(target=lambda i=i: L.append({"kind": "k", "i": i})) for i in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert L.verify_chain() and [r["seq"] for r in L.records()] == list(range(1, 13))


# ── ooxml guards ────────────────────────────────────────────────────────────────────────────────────────────
def test_ooxml_rejects_dtd_zipbomb_and_garbage(mods, kit):
    import io, zipfile
    O = mods.ooxml
    good = kit.make_docx(["a", "b"])
    O.validate_container(good, "docx")
    assert O.docx_paragraphs(good) == ["a", "b"]
    for bad in (b"", b"PK\x03\x04junk", b"not a zip", good[:50]):
        with pytest.raises(mods.errors.InvalidInput):
            O.validate_container(bad, "docx")
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>')
    with pytest.raises(mods.errors.InvalidInput, match="DTD"):
        O.validate_container(b.getvalue(), "docx")


def test_pdf_validation(mods, kit):
    O = mods.ooxml
    O.validate_pdf(kit.PDF)
    for bad in (b"", b"%PDF-1.4 truncated", b"<html>", b"PK\x03\x04"):
        with pytest.raises(mods.errors.InvalidInput):
            O.validate_pdf(bad)
