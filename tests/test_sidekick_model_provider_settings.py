"""Sidekick can change the model provider/key while the stack runs: approval-gated, by reference, tested, reversible."""
import os

import pytest

from agentic_os.app.llm import governed_text
from agentic_os.app.llm_settings import load_llm_settings
from agentic_os.mission.approval import ReplayGuard
from agentic_os.sidekick.capability import CapabilityRegistry
from agentic_os.sidekick.orchestrator import SidekickOrchestrator
from agentic_os.sidekick.settings_skill import (
    CAPABILITY_ID, bind_settings_capability, make_handler, mint_settings_approval)
from tests.test_llm_key_via_broker import _Server, _Strict  # local OpenAI-compatible servers

SECRET = "approval-secret-" + "x" * 32


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("REDEVOPS_SECRET_DIR", str(tmp_path / "secrets"))
    monkeypatch.setenv("REDEVOPS_SECRET_BACKEND", "file")
    monkeypatch.setenv("REDEVOPS_LLM_SETTINGS_FILE", str(tmp_path / "llm.json"))
    monkeypatch.setenv("AGENTIC_OS_APPROVAL_SECRET", SECRET)
    for k in ("REDEVOPS_LLM_BASE_URL", "REDEVOPS_LLM_MODEL", "OPENAI_API_KEY", "REDEVOPS_LLM_IN_BOUNDARY_HOSTS"):
        monkeypatch.delenv(k, raising=False)
    return tmp_path


@pytest.fixture
def strict():
    s = _Strict()
    yield s
    s.close()


def _h(**kw):
    return make_handler(replay_guard=ReplayGuard(), **kw)


def _approved(handler, **inputs):
    """The human approval round trip: preview -> approve exactly this payload -> re-invoke with the token."""
    first = handler(**inputs)
    assert first["status"] == "pending_approval", first
    change = first["payload"]
    token = mint_settings_approval(change)
    return handler(**inputs, _approval=token), token


def test_show_and_test_need_no_approval_and_change_nothing(env, strict):
    h = _h()
    out = h(action="show")
    assert out["status"] == "ok" and out["boundary"] == "unset" and not (env / "llm.json").exists()


def test_a_key_is_never_accepted_as_an_input(env):
    out = _h()(action="set", base_url="http://127.0.0.1:1/v1", model="m", api_key="sk-live-should-not-be-here")
    assert out["status"] == "refused" and "sk-live-should-not-be-here" not in str(out)
    assert out["rejected_inputs"] == ["api_key"]


def test_a_change_without_approval_is_only_a_preview(env, strict):
    out = _h()(action="set", base_url=strict.url, model="m", key_ref="file:llm/provider", persist="store")
    assert out["status"] == "pending_approval" and out["nothing_changed"] and len(out["payload_digest"]) == 64
    assert out["preview"]["boundary"].startswith("inside") and not (env / "llm.json").exists()


def test_an_external_host_needs_an_explicit_statement_and_the_preview_says_it_leaves_the_boundary(env):
    h = _h()
    refused = h(action="set", base_url="https://api.openai.com/v1", model="gpt", key_ref="file:llm/p", persist="store")
    assert refused["status"] == "refused" and "trust boundary" in refused["reason"]
    pending = h(action="set", base_url="https://api.openai.com/v1", model="gpt", key_ref="file:llm/p",
                persist="store", allow_external_host=True)
    assert pending["status"] == "pending_approval" and pending["preview"]["boundary"].startswith("OUTSIDE")


def test_approved_set_stores_the_key_by_reference_tests_it_and_it_is_live(env, strict, monkeypatch):
    monkeypatch.setenv("MY_PROVIDER_KEY", "good-key-7777")
    out, _ = _approved(_h(), action="set", base_url=strict.url, model="m", key_ref="file:llm/provider",
                       persist="store", key_env="MY_PROVIDER_KEY")
    assert out["status"] == "applied" and out["receipt"]["test"]["ok"]
    assert "good-key-7777" not in str(out) and "good-key-7777" not in (env / "llm.json").read_text()
    assert (env / "secrets" / "llm" / "provider").read_text() == "good-key-7777"
    assert governed_text("hi") == "pong"                                    # live, no restart


def test_the_approval_is_bound_to_the_exact_payload_and_single_use(env, strict, monkeypatch):
    monkeypatch.setenv("MY_PROVIDER_KEY", "good-key-1")
    h = _h()
    out, token = _approved(h, action="set", base_url=strict.url, model="m", key_ref="file:llm/provider",
                           persist="store", key_env="MY_PROVIDER_KEY")
    assert out["status"] == "applied"
    again = h(action="set", base_url=strict.url, model="m", key_ref="file:llm/provider", persist="store",
              key_env="MY_PROVIDER_KEY", _approval=token)                  # replay of the same token
    assert again["status"] == "pending_approval"
    tampered = h(action="set", base_url=strict.url, model="OTHER", key_ref="file:llm/provider", persist="store",
                 key_env="MY_PROVIDER_KEY", _approval=token)               # token minted for a different payload
    assert tampered["status"] == "pending_approval"


def test_a_bad_rotation_is_rolled_back_to_the_previous_working_key(env, strict, monkeypatch):
    h = _h()
    monkeypatch.setenv("K1", "good-old-0001")
    _approved(h, action="set", base_url=strict.url, model="m", key_ref="file:llm/provider", persist="store", key_env="K1")
    monkeypatch.setenv("K2", "BAD-new")
    out, _ = _approved(h, action="rotate_key", persist="store", key_env="K2")
    assert out["status"] == "rolled_back" and out["receipt"]["test"]["status"] == 401
    assert governed_text("hi") == "pong"                                    # still the old key
    assert (env / "secrets" / "llm" / "provider").read_text() == "good-old-0001"


def test_env_ref_persists_only_a_reference_and_writes_no_secret(env, strict, monkeypatch):
    monkeypatch.setenv("EXPORTED_KEY", "good-exported-key")
    out, _ = _approved(_h(), action="set", base_url=strict.url, model="m", key_env="EXPORTED_KEY", persist="env_ref")
    assert out["status"] == "applied"
    assert load_llm_settings().key_ref == "env:EXPORTED_KEY"
    assert not (env / "secrets").exists()                                   # nothing written anywhere
    assert governed_text("hi") == "pong"


def test_missing_env_var_and_missing_key_source_are_refused(env, strict, monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    out, _ = _approved(_h(), action="set", base_url=strict.url, model="m", key_ref="file:llm/provider",
                       persist="store", key_env="NOPE")
    assert out["status"] == "refused" and "NOPE" in out["reason"]
    out2, _ = _approved(_h(), action="set", base_url=strict.url, model="m", key_ref="file:llm/provider", persist="store")
    assert out2["status"] == "refused" and "key source" in out2["reason"]


def test_the_secure_prompt_supplies_the_key_without_it_being_an_input(env, strict):
    asked = []
    h = _h(prompt_secret=lambda label: asked.append(label) or "good-prompted-key")
    out, _ = _approved(h, action="set", base_url=strict.url, model="m", key_ref="file:llm/provider", persist="store")
    assert out["status"] == "applied" and "file:llm/provider" in asked[0]
    assert (env / "secrets" / "llm" / "provider").read_text() == "good-prompted-key"


def test_it_is_discoverable_and_invocable_through_the_sidekick_orchestrator(env):
    reg = CapabilityRegistry()
    assert bind_settings_capability(reg, replay_guard=ReplayGuard()) == CAPABILITY_ID
    orch = SidekickOrchestrator(registry=reg)
    hit = orch.discover("rotate the model key please")[0]
    assert hit.capability.capability_id == CAPABILITY_ID and hit.capability.bound
    res = orch.invoke(CAPABILITY_ID, action="show")
    assert res.data["status"] == "ok"
