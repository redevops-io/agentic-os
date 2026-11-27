"""The name the Support app speaks for is the installation's, not the demo tenant's: it is written into text a customer reads."""
from __future__ import annotations

import importlib

import pytest

DEMO = "Meridian Wealth Management"


@pytest.fixture()
def core_for(monkeypatch):
    mod = importlib.import_module("support.core")

    def load(**env):
        for k in ("SUPPORT_TENANT", "SUPPORT_TENANT_DESCRIPTION", "SUPPORT_TOPICS"):
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        return importlib.reload(mod)
    yield load
    for k in ("SUPPORT_TENANT", "SUPPORT_TENANT_DESCRIPTION", "SUPPORT_TOPICS"):
        monkeypatch.delenv(k, raising=False)
    importlib.reload(mod)


def test_without_configuration_the_app_is_the_demo_tenant_as_before(core_for):
    core = core_for()
    text = core._welcome_text("Ada Lovelace", None)
    assert core.TENANT == DEMO and text.count(DEMO) == 2 and "advisory fees" in text and "returns" in core.NO_INVENTION


def test_a_configured_tenant_is_named_in_the_welcome_message_and_nothing_of_the_demo_firm_remains(core_for):
    core = core_for(SUPPORT_TENANT="Northside Cycles")
    text = core._welcome_text("Ada Lovelace", "Pro")
    assert text.count("Northside Cycles") == 2 and "Hi Ada" in text and "Pro plan" in text
    assert DEMO not in text and "advisory" not in text and "statements" not in text and "your account" in text
    assert core.TENANT_DESCRIPTION == "" and "returns" not in core.NO_INVENTION, "nothing about the tenant is invented when it was not given"


def test_the_description_and_topics_are_used_only_when_given(core_for):
    core = core_for(SUPPORT_TENANT="Northside Cycles", SUPPORT_TENANT_DESCRIPTION="a bicycle repair shop", SUPPORT_TOPICS="repairs, parts or your booking")
    assert "repairs, parts or your booking" in core._welcome_text("Ada", None) and core.TENANT_DESCRIPTION == "a bicycle repair shop"


def test_the_draft_names_the_configured_tenant_to_the_model_and_in_the_template(core_for, monkeypatch):
    core = core_for(SUPPORT_TENANT="Northside Cycles", SUPPORT_TENANT_DESCRIPTION="a bicycle repair shop")
    conv = {"id": 7, "meta": {"sender": {"name": "Ada Lovelace"}, "channel": "Channel::Api"}, "messages": [{"message_type": 0, "content": "My brake squeaks."}]}
    monkeypatch.setattr(core, "_get_conversation", lambda cid: conv)
    posted = []

    class Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, url, headers=None, json=None):
            posted.append(json)

            class R:
                status_code = 200
            return R()
    monkeypatch.setattr(core.httpx, "Client", Client)
    prompts = []
    out = core.draft_reply({"conversation_id": 7}, blurb=lambda prompt: prompts.append(prompt))          # the model gives nothing: the template is used
    assert "Northside Cycles, a bicycle repair shop" in prompts[0] and DEMO not in prompts[0] and "returns" not in prompts[0]
    assert "Northside Cycles" in out["draft"] and DEMO not in out["draft"]
    assert posted and posted[0]["private"] is True and "Northside Cycles" in posted[0]["content"] and DEMO not in posted[0]["content"]
