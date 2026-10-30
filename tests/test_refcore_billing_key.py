"""The reference billing core and its compose files carry no default credential.

The service must refuse to start without an explicitly supplied REFCORE_API_KEY (absent, empty, whitespace-only or the
retired development key), compares the key in constant time, and the compose files require the values instead of
defaulting them. Synthetic keys only.
"""
from __future__ import annotations

import hmac
import importlib.util
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "deploy" / "refcore-billing" / "app.py"
REFCORE_COMPOSE = ROOT / "deploy" / "refcore-billing" / "refcore.compose.yml"
ADMIN_COMPOSE = ROOT / "deploy" / "admin-console" / "admin.compose.yml"
RETIRED = "refcore-dev-key"
KEY = "synthetic-refcore-key-for-tests-only"


def _load(monkeypatch, key=None, db=None):
    if key is None:
        monkeypatch.delenv("REFCORE_API_KEY", raising=False)
    else:
        monkeypatch.setenv("REFCORE_API_KEY", key)
    if db is not None:
        monkeypatch.setenv("REFCORE_DB", str(db))
    spec = importlib.util.spec_from_file_location("refcore_billing_app_under_test", APP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── startup ─────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("key", [None, "", " ", "\t\n", RETIRED, f" {RETIRED} ", f"{RETIRED}\n"])
def test_the_service_refuses_to_start_without_an_explicit_key(monkeypatch, key):
    with pytest.raises(RuntimeError) as e:
        _load(monkeypatch, key)
    assert "REFCORE_API_KEY" in str(e.value)


def test_the_refusal_message_does_not_contain_a_supplied_key(monkeypatch):
    # the message may name the retired default as a reason, but never echoes the value that was supplied
    with pytest.raises(RuntimeError) as e:
        _load(monkeypatch, f" {RETIRED} ")
    assert f" {RETIRED} " not in str(e.value) and "retired" in str(e.value)


def test_an_explicit_key_starts_the_service(monkeypatch, tmp_path):
    assert _load(monkeypatch, KEY, tmp_path / "c.json").app is not None


# ── authentication ──────────────────────────────────────────────────────────────────────────────────────
def _client(monkeypatch, tmp_path):
    return TestClient(_load(monkeypatch, KEY, tmp_path / "data" / "customers.json").app)


def test_only_the_configured_key_is_accepted(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    body = {"customer": {"external_id": "cust-1", "name": "Synthetic Co"}}
    for bad in (None, "", "Bearer", "Bearer ", f"Bearer {RETIRED}", f"Bearer {KEY[:-1]}", f"Bearer {KEY}x",
                f"bearer {KEY}", KEY, f"Basic {KEY}"):
        h = {} if bad is None else {"Authorization": bad}
        assert c.get("/api/v1/customers", headers=h).status_code == 401, bad
        assert c.post("/api/v1/customers", headers=h, json=body).status_code == 401, bad
    ok = {"Authorization": f"Bearer {KEY}"}
    assert c.post("/api/v1/customers", headers=ok, json=body).status_code == 200
    assert [x["external_id"] for x in c.get("/api/v1/customers", headers=ok).json()["customers"]] == ["cust-1"]
    assert c.get("/healthz").status_code == 200                       # liveness stays open


def test_a_non_ascii_credential_is_401_not_a_server_error(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    r = c.get("/api/v1/customers", headers={"Authorization": "Bearer kéy".encode("latin-1")})
    assert r.status_code == 401


def test_the_key_is_compared_with_hmac_compare_digest(monkeypatch, tmp_path):
    calls = []
    real = hmac.compare_digest
    monkeypatch.setattr(hmac, "compare_digest", lambda a, b: calls.append((a, b)) or real(a, b))
    c = _client(monkeypatch, tmp_path)
    c.get("/api/v1/customers", headers={"Authorization": f"Bearer {KEY}"})
    c.get("/api/v1/customers", headers={"Authorization": "Bearer wrong"})
    assert len(calls) == 2 and all(isinstance(a, bytes) and isinstance(b, bytes) for a, b in calls)


# ── nothing in the repository carries or defaults the retired key ───────────────────────────────────────
def test_the_retired_key_appears_only_in_the_refusal_list():
    hits = []
    for base in ("deploy", "docs", "scripts", "apps", "agentic_os", "examples", "integrations", "packaging"):
        for p in (ROOT / base).rglob("*"):
            if p.is_file() and p.suffix in {".py", ".md", ".yml", ".yaml", ".sh", ".env", ".json", ".toml", ".txt", ""} \
                    and "node_modules" not in p.parts and "__pycache__" not in p.parts:
                try:
                    if RETIRED in p.read_text(encoding="utf-8"):
                        hits.append(p.relative_to(ROOT).as_posix())
                except (UnicodeDecodeError, OSError):
                    continue
    for top in ROOT.glob("*"):
        # CHANGELOG.md names the retired key in the breaking-change note
        if top.is_file() and top.suffix in {".md", ".yml", ".yaml", ".toml"} and top.name != "CHANGELOG.md" \
                and RETIRED in top.read_text(encoding="utf-8"):
            hits.append(top.name)
    assert hits == ["deploy/refcore-billing/app.py"]


def test_the_compose_files_require_the_values_and_default_none_of_them():
    ref = REFCORE_COMPOSE.read_text()
    assert re.search(r"REFCORE_API_KEY=\$\{REFCORE_API_KEY:\?[^}]+\}", ref)
    adm = ADMIN_COMPOSE.read_text()
    assert re.search(r"LAGO_API_URL=\$\{LAGO_API_URL:\?[^}]+\}", adm)
    assert re.search(r"LAGO_API_KEY=\$\{LAGO_API_KEY:\?[^}]+\}", adm)
    for text in (ref, adm):
        for name in ("REFCORE_API_KEY", "LAGO_API_KEY", "LAGO_API_URL"):
            assert not re.search(r"\$\{%s:?-" % name, text), f"{name} must not have a default"
        assert not re.search(r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d+\.\d+(?:\.\d+)?\b", text), "no private address default"
        assert RETIRED not in text


def test_the_images_do_not_bake_a_key():
    for df in (ROOT / "deploy" / "refcore-billing" / "Dockerfile", ROOT / "deploy" / "admin-console" / "Dockerfile"):
        assert not re.search(r"(?im)^\s*(ENV|ARG)\s+(REFCORE_API_KEY|LAGO_API_KEY)\b", df.read_text())
