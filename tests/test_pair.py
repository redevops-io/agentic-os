"""NVIDIA PAIR detection + its place in the default-LLM resolver."""
from agentic_os.mission.pair import detect_pair, PairStatus, PAIR_OPENAI_BASE
from agentic_os.mission.default_llm import resolve_llm


# ── detector ────────────────────────────────────────────────────────────────────────────

def test_detect_parses_openai_model_inventory():
    def fetch(url, timeout):
        assert url.endswith("/v1/models")
        return {"data": [{"id": "qwen2.5:14b"}, {"id": "llama3.3:70b"}]}
    s = detect_pair(fetch=fetch)
    assert s.available and s.base_url == PAIR_OPENAI_BASE
    assert s.models == ("qwen2.5:14b", "llama3.3:70b")
    assert s.default_model == "qwen2.5:14b"


def test_detect_absent_when_endpoint_unreachable():
    def boom(url, timeout):
        raise ConnectionRefusedError("nothing on :1234")
    s = detect_pair(fetch=boom)
    assert not s.available and s.default_model == ""


def test_detect_available_with_empty_inventory():
    s = detect_pair(fetch=lambda u, t: {"data": []})
    assert s.available and s.models == () and s.default_model == ""


# ── resolver ordering: user creds → PAIR → local → guided ─────────────────────────────────

def _pair_up(model="qwen2.5:14b"):
    return lambda: PairStatus(available=True, base_url=PAIR_OPENAI_BASE, models=(model,))

def _pair_down():
    return lambda: PairStatus(available=False)


def test_pair_used_when_no_creds():
    c = resolve_llm(env={}, pair_detect=_pair_up("qwen2.5:14b"))
    assert c.source == "pair" and c.provider == "pair"
    assert c.base_url == PAIR_OPENAI_BASE and c.model == "qwen2.5:14b"
    assert not c.needs_setup


def test_user_creds_win_over_pair():
    c = resolve_llm(env={"GROQ_API_KEY": "x"}, pair_detect=_pair_up())
    assert c.source == "user_creds" and c.provider == "groq"


def test_pair_before_direct_local():
    # both PAIR and a MODEL_ENDPOINT exist → PAIR wins (the stable fabric abstraction)
    c = resolve_llm(env={"MODEL_ENDPOINT": "http://x:8000/v1"}, pair_detect=_pair_up())
    assert c.source == "pair"


def test_falls_through_to_local_when_pair_absent():
    c = resolve_llm(env={"MODEL_ENDPOINT": "http://x:8000/v1"}, pair_detect=_pair_down())
    assert c.source == "local" and c.base_url == "http://x:8000/v1"


def test_falls_through_to_guided_when_nothing():
    c = resolve_llm(env={}, pair_detect=_pair_down())
    assert c.source == "guided" and c.provider == "groq" and c.needs_setup


def test_pair_can_be_disabled():
    c = resolve_llm(env={"RDO_DISABLE_PAIR": "1"}, pair_detect=_pair_up())
    assert c.source == "guided"        # PAIR skipped despite being available
