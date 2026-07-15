from agentic_os.router import Router, Tier, Task

def _router():
    return Router([
        Tier("local", "http://x/v1", "qwen", frozenset({"draft", "extract"}), cost_per_1k=0.0),
        Tier("premium", "http://y/v1", "gpt-5-codex", frozenset({"hard-reason"}), cost_per_1k=0.02),
    ], monthly_budget_usd=10)

def test_selects_cheapest_capable_tier():
    r = _router()
    assert r.select("draft").name == "local"
    assert r.select("hard-reason").name == "premium"

def test_unknown_capability_raises():
    import pytest
    with pytest.raises(LookupError):
        _router().select("telepathy")
