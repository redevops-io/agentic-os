"""The LLM key and provider are resolved per call through the broker, so changing them needs no restart or
reinstall (the Sidekick settings flow). A local OpenAI-compatible server records what it receives."""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agentic_os.app.llm import default_in_boundary_endpoint, governed_text
from agentic_os.app.llm_settings import (
    SETTINGS_FILE_ENV, SettingsError, llm_api_key, load_llm_settings, save_llm_settings)
from agentic_os.secrets import store_secret
from agentic_os.secrets.resolve import SecretReferenceError


class _Server:
    def __init__(self, tag):
        self.seen = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.seen.append({"auth": self.headers.get("Authorization"), "model": body["model"]})
                out = json.dumps({"choices": [{"message": {"content": f"hello from {tag}"}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def a():
    s = _Server("A")
    yield s
    s.close()


@pytest.fixture
def b():
    s = _Server("B")
    yield s
    s.close()


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("REDEVOPS_SECRET_DIR", str(tmp_path / "secrets"))
    monkeypatch.setenv(SETTINGS_FILE_ENV, str(tmp_path / "llm.json"))
    for k in ("REDEVOPS_LLM_BASE_URL", "REDEVOPS_LLM_MODEL", "OPENAI_API_KEY", "REDEVOPS_LLM_IN_BOUNDARY_HOSTS"):
        monkeypatch.delenv(k, raising=False)
    return tmp_path / "llm.json"


def test_rotating_the_key_in_the_store_is_live_on_the_next_call(cfg, a):
    store_secret("file:llm/provider", "key-1")
    save_llm_settings(str(cfg), base_url=a.url, model="m1", key_ref="file:llm/provider")
    assert governed_text("hi") == "hello from A"
    store_secret("file:llm/provider", "key-2")                       # Sidekick rotates the key: just a store write
    assert governed_text("hi") == "hello from A"
    assert [r["auth"] for r in a.seen] == ["Bearer key-1", "Bearer key-2"]


def test_switching_provider_and_model_is_live_without_a_restart(cfg, a, b):
    store_secret("file:llm/provider", "k")
    save_llm_settings(str(cfg), base_url=a.url, model="small", key_ref="file:llm/provider")
    assert governed_text("hi") == "hello from A"
    save_llm_settings(str(cfg), base_url=b.url, model="big", key_ref="file:llm/provider")     # settings change
    assert governed_text("hi") == "hello from B"
    assert a.seen[-1]["model"] == "small" and b.seen[-1]["model"] == "big"


def test_the_settings_file_never_holds_the_key(cfg, a):
    store_secret("file:llm/provider", "super-secret-value")
    save_llm_settings(str(cfg), base_url=a.url, model="m", key_ref="file:llm/provider")
    text = cfg.read_text()
    assert "super-secret-value" not in text and "file:llm/provider" in text
    assert oct(os.stat(cfg).st_mode & 0o777) == "0o600"


def test_an_unreadable_reference_degrades_and_never_falls_back_to_another_credential(cfg, a, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-key-must-not-be-used")
    save_llm_settings(str(cfg), base_url=a.url, model="m", key_ref="file:llm/missing")
    assert llm_api_key(load_llm_settings()) == "EMPTY"


def test_a_public_provider_is_still_refused_until_its_host_is_explicitly_allowed(cfg):
    save_llm_settings(str(cfg), base_url="https://api.openai.com/v1", model="gpt-x", key_ref="")
    assert default_in_boundary_endpoint() is None and governed_text("hi") is None     # fail closed
    save_llm_settings(str(cfg), base_url="https://api.openai.com/v1", model="gpt-x", key_ref="",
                      in_boundary_hosts=("api.openai.com",))
    ep = default_in_boundary_endpoint()
    assert ep is not None and ep.model_id == "gpt-x"                                   # an explicit allow


def test_with_no_settings_file_the_environment_works_exactly_as_before(cfg, a, monkeypatch):
    monkeypatch.setenv("REDEVOPS_LLM_BASE_URL", a.url)
    monkeypatch.setenv("REDEVOPS_LLM_MODEL", "env-model")
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    assert not cfg.exists() and load_llm_settings().source == "env"
    assert governed_text("hi") == "hello from A"
    assert a.seen[-1] == {"auth": "Bearer env-key", "model": "env-model"}


def test_a_group_writable_settings_file_is_ignored(cfg, a, monkeypatch):
    save_llm_settings(str(cfg), base_url=a.url, model="from-file")
    os.chmod(cfg, 0o664)
    monkeypatch.setenv("REDEVOPS_LLM_BASE_URL", a.url)
    monkeypatch.setenv("REDEVOPS_LLM_MODEL", "from-env")
    assert load_llm_settings().source == "env" and load_llm_settings().model == "from-env"


def test_bad_inputs_are_refused_before_anything_is_written(cfg):
    with pytest.raises(SettingsError):
        save_llm_settings(str(cfg), base_url="ftp://x", model="m")
    with pytest.raises(SettingsError):
        save_llm_settings(str(cfg), base_url="http://127.0.0.1:1/v1", model=" ")
    with pytest.raises(SecretReferenceError):
        save_llm_settings(str(cfg), base_url="http://127.0.0.1:1/v1", model="m", key_ref="no-colon")
    assert not cfg.exists()


# ── the change operation a shell (Sidekick) performs: store -> settings -> test -> keep or roll back ──────────────
class _Strict(_Server):
    """Accepts only 'good-*' keys; anything else is a 401."""
    def __init__(self):
        outer = self
        self.seen = []

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                auth = self.headers.get("Authorization", "")
                outer.seen.append(auth)
                ok = auth.startswith("Bearer good-")
                out = json.dumps({"choices": [{"message": {"content": "pong"}}]} if ok else {"error": "bad key"}).encode()
                self.send_response(200 if ok else 401)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


@pytest.fixture
def strict():
    s = _Strict()
    yield s
    s.close()


def test_set_provider_stores_tests_and_keeps_a_working_key(cfg, strict):
    from agentic_os.app.llm_admin import set_provider
    r = set_provider(str(cfg), base_url=strict.url, model="m", key_ref="file:llm/provider", key_value="good-key-111122")
    assert r["changed"] and r["test"]["ok"] and r["key"] == "set (…1122)" and "good-key" not in str(r)
    assert governed_text("hi") == "pong"


def test_a_bad_new_key_is_rolled_back_to_the_previous_working_one(cfg, strict):
    from agentic_os.app.llm_admin import rotate_key, set_provider
    set_provider(str(cfg), base_url=strict.url, model="m", key_ref="file:llm/provider", key_value="good-old-0000")
    r = rotate_key(str(cfg), new_key="BAD-new-key")
    assert r["changed"] is False and r["rolled_back"] is True and r["test"]["status"] == 401
    assert governed_text("hi") == "pong"                                   # still works with the OLD key
    assert strict.seen[-1] == "Bearer good-old-0000"


def test_a_good_rotation_is_kept_and_live(cfg, strict):
    from agentic_os.app.llm_admin import rotate_key, set_provider
    set_provider(str(cfg), base_url=strict.url, model="m", key_ref="file:llm/provider", key_value="good-old-0000")
    assert rotate_key(str(cfg), new_key="good-new-9999")["changed"]
    governed_text("hi")
    assert strict.seen[-1] == "Bearer good-new-9999"


def test_a_failed_first_setup_leaves_nothing_behind(cfg, strict):
    from agentic_os.app.llm_admin import set_provider
    r = set_provider(str(cfg), base_url=strict.url, model="m", key_ref="file:llm/provider", key_value="nope")
    assert r["rolled_back"] and not cfg.exists()


def test_an_external_provider_needs_explicit_approval_and_says_what_it_means(cfg):
    from agentic_os.app.llm_admin import ProviderChangeRefused, set_provider
    with pytest.raises(ProviderChangeRefused, match="trust boundary"):
        set_provider(str(cfg), base_url="https://api.openai.com/v1", model="gpt", key_ref="file:llm/p", key_value="k",
                     test=False)
    assert not cfg.exists()
    r = set_provider(str(cfg), base_url="https://api.openai.com/v1", model="gpt", key_ref="file:llm/p", key_value="k",
                     allow_external_host=True, test=False)
    assert r["changed"] and r["boundary"].startswith("EXTERNAL") and "leaves your trust boundary" in r["note"]
    assert load_llm_settings().in_boundary_hosts == ("api.openai.com",)


def test_rotate_without_a_stored_reference_is_refused(cfg):
    from agentic_os.app.llm_admin import ProviderChangeRefused, rotate_key
    with pytest.raises(ProviderChangeRefused):
        rotate_key(str(cfg), new_key="x")
