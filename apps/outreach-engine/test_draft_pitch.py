"""outreach-engine's LLM-drafted pitch (evidence visualization + the lead's real link).

`core.pitch_visual_html` / `core.render_pitch_html` are deterministic (no LLM; core.py never touches it) and
tested directly. `app_mod.draft_pitch` is the governed-LLM action — these tests force `_llm` both ways
(empty -> template fallback; a value -> the LLM path) rather than ever calling a real model.
"""
from __future__ import annotations

import importlib

import pytest

core = importlib.import_module("outreach-engine.core")
app_mod = importlib.import_module("outreach-engine.app")


@pytest.fixture(autouse=True)
def _reset_state():
    core._STATE.update(accounts=[], approved=set(), candidates=[])
    yield


def test_pitch_visual_html_renders_real_signal_fields_only():
    row = {"signal": "tech_pain", "source": "github", "stars": 2500}
    out = core.pitch_visual_html(row)
    assert "<table" in out and "WHY WE REACHED OUT" in out
    # 1.0 * 1.0 signal/source weight for tech_pain+github, and 2500/5000 = 50% traction
    assert "100%" in out and "50%" in out


def test_pitch_visual_html_omits_traction_bar_without_stars():
    row = {"signal": "cold", "source": "manual"}
    out = core.pitch_visual_html(row)
    assert "Project traction" not in out


def test_render_pitch_html_escapes_text_and_includes_the_real_link():
    row = {"url": "https://github.com/acme/repo", "signal": "cold", "source": "manual"}
    out = core.render_pitch_html("subj", "Hi <there> & welcome.\n\nSecond para.", row)
    assert "&lt;there&gt;" in out and "&amp;" in out      # escaped, never raw HTML injection
    assert 'href="https://github.com/acme/repo"' in out
    assert out.count("<p ") == 3                         # 2 body paragraphs + the link paragraph


def test_render_pitch_html_has_no_link_when_the_row_has_none():
    out = core.render_pitch_html("subj", "body text", {"signal": "cold", "source": "manual"})
    assert "<a href" not in out


def test_draft_pitch_unknown_account():
    res = app_mod.draft_pitch({"account": "nope"})
    assert res["ok"] is False and "refresh the pipeline" in res["reason"]


def test_draft_pitch_falls_back_to_template_when_llm_returns_nothing(monkeypatch):
    monkeypatch.setattr(app_mod, "_llm", lambda *a, **k: "")
    core._STATE["accounts"] = [dict(core.DEMO[0])]
    acct = core.DEMO[0]["account"]
    res = app_mod.draft_pitch({"account": acct})
    assert res["ok"] is True and res["draft_source"] == "template"
    tmpl = core.teardown(core.DEMO[0])
    assert res["body"] == tmpl["body"] and res["subject"] == tmpl["subject"]
    assert "<table" in res["html"]
    row = core._STATE["accounts"][0]
    assert row["draft"]["body"] == tmpl["body"] and row["draft"]["draft_source"] == "template"


def test_draft_pitch_uses_the_llm_when_it_answers(monkeypatch):
    seen_prompts = []

    def fake_llm(prompt, max_tokens=400):
        seen_prompts.append(prompt)
        return "  A short, LLM-written pitch tailored to this lead.  "

    monkeypatch.setattr(app_mod, "_llm", fake_llm)
    row = {"account": "acme-oss", "signal": "tech_pain", "source": "github", "stars": 900,
          "evidence": "builds RAG in acme-oss/acme (900★)", "url": "https://github.com/acme-oss/acme"}
    core._STATE["accounts"] = [row]
    res = app_mod.draft_pitch({"account": "acme-oss"})
    assert res["ok"] is True and res["draft_source"] == "llm"
    assert res["body"] == "A short, LLM-written pitch tailored to this lead."   # stripped, not the raw LLM text
    assert 'href="https://github.com/acme-oss/acme"' in res["html"]
    assert any("builds RAG in acme-oss/acme" in p for p in seen_prompts)       # the real evidence was grounded in the prompt
    assert row["draft"]["body"] == res["body"]


def test_draft_pitch_is_registered_in_the_route_dispatch():
    assert app_mod._ACTIONS["draft_pitch"] is app_mod.draft_pitch
    assert app_mod._TOOL_OF["draft_pitch"] == "outreach.draft_pitch"
