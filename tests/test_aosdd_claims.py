"""Claims about verification must be accurate (v3 item 3): ERPNext reader role and the Twenty read-only key are server-enforced; Chatwoot and Lago use
independent GET-only read-back code but SHARE THE WRITER'S CREDENTIAL and are never called read-only verification / application-enforced."""
from __future__ import annotations

import glob
import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from aosdd_fakes import config_dict, make_config, reader_dict  # noqa: E402
from test_aosdd_runtime import Env, go  # noqa: E402

from agentic_os.integrations.aosdd.config import READ_ONLY_KIND, AosddConfig, ConfigError
from agentic_os.integrations.aosdd.reconstruct import reconstruct
from agentic_os.integrations.aosdd.verify import READER_NOTE

PKG = os.path.dirname(__import__("agentic_os.integrations.aosdd", fromlist=["x"]).__file__)
FORBIDDEN = [r"application[- ]enforced read[- ]only", r"read[- ]only verif(?:ication|ier|y)", r"read[- ]only (?:credential|key|role|user|token)", r"scoped (?:key|credential|token)"]
NEGATION = re.compile(r"\b(no|not|never|without|nor|neither|cannot|isn't|none|offers? no|has no|have no|lacks?)\b|NOT|not server-enforced")
SERVER_ENFORCED_APPS = ("erpnext", "twenty")


def _sentences(text: str):
    flat = re.sub(r"\s+", " ", re.sub(r"(?m)^\s*#:?", " ", text))
    for m in re.finditer(r"[^.;]*", flat):
        if m.group().strip():
            yield m.group()


def test_the_vocabulary_has_exactly_the_two_kinds_and_names_the_shared_credential():
    assert set(READ_ONLY_KIND.values()) == {"server-enforced", "get-only-by-code-shared-writer-credential"}
    assert {a for a, k in READ_ONLY_KIND.items() if k == "server-enforced"} == set(SERVER_ENFORCED_APPS)
    for app in ("chatwoot", "lago"):
        assert READ_ONLY_KIND[app] == "get-only-by-code-shared-writer-credential"
        assert "read-only" not in READER_NOTE[app].lower().replace("read-only-ness", "") and "not server-enforced" in READER_NOTE[app]
        assert "writer's own" in READER_NOTE[app]
    for app in SERVER_ENFORCED_APPS:
        assert "server-enforced" in READER_NOTE[app]


def test_no_forbidden_phrasing_about_the_shared_credential_apps_anywhere_in_the_package_or_tests():
    files = glob.glob(PKG + "/*.py") + [f for f in glob.glob(os.path.dirname(__file__) + "/*aosdd*.py") if not f.endswith("test_aosdd_claims.py")]
    bad = []
    for f in files:
        for sent in _sentences(open(f).read()):
            for pat in FORBIDDEN:
                if re.search(pat, sent, re.I) and not NEGATION.search(sent) and not any(a in sent.lower() for a in ("erpnext", "twenty")):
                    bad.append((os.path.basename(f), sent.strip()[:140]))
    assert not bad, bad


def test_a_phrase_about_chatwoot_or_lago_next_to_a_forbidden_term_is_flagged_by_the_scanner():
    flagged = [s for s in _sentences("Chatwoot uses a read-only credential for verification. Lago has no read-only role.")
               if any(re.search(p, s, re.I) for p in FORBIDDEN) and not NEGATION.search(s)]
    assert len(flagged) == 1 and "Chatwoot uses a read-only credential" in flagged[0]       # the scanner catches the wrong claim and lets the negation through


def test_receipts_state_the_kind_and_the_shared_credential_and_never_a_read_only_claim_for_chatwoot_or_lago(tmp_path):
    env = Env(tmp_path)
    r, mid = go(env, "CLAIMS")
    rec = reconstruct(env.events_path, mid)
    text = json.dumps(rec)
    for s in rec["steps"]:
        v = s["verification"]
        assert set(v) >= {"credential_source", "read_only_kind", "same_secret_as_writer"}
        if s["app"] in SERVER_ENFORCED_APPS:
            assert v["read_only_kind"] == "server-enforced" and v["same_secret_as_writer"] is False
        else:
            assert v["read_only_kind"] == "get-only-by-code-shared-writer-credential" and v["same_secret_as_writer"] is True
        assert "reader" in v and (s["app"] in SERVER_ENFORCED_APPS or "not server-enforced" in v["reader"])
    assert {a: k["read_only"] for a, k in rec["verifier"]["apps"].items()} == READ_ONLY_KIND
    for pat in FORBIDDEN:
        for m in re.finditer(pat, text, re.I):
            ctx = text[max(0, m.start() - 160): m.end() + 40].lower()
            assert "erpnext" in ctx or "twenty" in ctx or NEGATION.search(ctx), ctx


def test_a_distinct_verifier_secret_for_chatwoot_or_lago_is_refused_because_it_would_only_look_scoped():
    rd = reader_dict()
    rd["chatwoot"] = {"token": "a-different-chatwoot-agent-token-0001"}
    with pytest.raises(ConfigError, match="no server-enforced read-only credential"):
        AosddConfig.from_dict(config_dict(), approval_secret="s" * 40, reader=rd)
    rd = reader_dict()
    rd["lago"] = {"api_key": "a-different-lago-key-0002"}
    with pytest.raises(ConfigError, match="lago"):
        AosddConfig.from_dict(config_dict(), approval_secret="s" * 40, reader=rd)
