"""Default-LLM resolver — pick a model for the first bootstrap steps without shipping a key."""
from agentic_os.mission.default_llm import resolve_llm, REGISTRY


def test_no_creds_no_local_gives_guided_free_tier():
    c = resolve_llm(env={})
    assert c.source == "guided" and c.needs_setup
    assert c.provider == "groq"                      # top pick: no card, does not train
    assert c.signup_url and c.base_url
    assert c.alternatives                            # other free options offered
    assert not c.trains_on_data


def test_user_provider_key_wins():
    c = resolve_llm(env={"GROQ_API_KEY": "x"})
    assert c.source == "user_creds" and c.provider == "groq"
    assert c.api_key_env == "GROQ_API_KEY" and not c.needs_setup


def test_bring_your_own_endpoint():
    c = resolve_llm(env={"RDO_LLM_BASE_URL": "http://llm:8000/v1", "RDO_LLM_API_KEY": "k",
                         "RDO_LLM_MODEL": "my-model"})
    assert c.source == "user_creds" and c.provider == "custom"
    assert c.base_url == "http://llm:8000/v1" and c.model == "my-model"


def test_local_model_preferred_over_guided():
    c = resolve_llm(env={"MODEL_ENDPOINT": "http://host.docker.internal:8000/v1"})
    assert c.source == "local" and not c.needs_setup
    assert c.base_url.endswith(":8000/v1")


def test_prefer_reorders_the_guided_recommendation():
    c = resolve_llm(env={}, prefer=("gemini",))
    assert c.source == "guided" and c.provider == "gemini"
    assert c.trains_on_data                          # flagged so a governed install can avoid it


def test_registry_flags_are_honest():
    by_id = {p.id: p for p in REGISTRY}
    assert by_id["gemini"].trains_on_data and not by_id["gemini"].needs_card
    assert not by_id["groq"].trains_on_data
    assert "x.ai" in by_id["xai"].base_url
