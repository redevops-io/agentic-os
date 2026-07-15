from agentic_os import Registry

def test_catalog_loads_and_validates():
    reg = Registry.load()
    assert len(reg) >= 10
    assert "agentic-os" not in reg.names          # the OS does not list itself as a module
    billing = reg.get("agentic-billing")
    assert billing.url == "https://github.com/redevops-io/agentic-billing"
    assert billing.needs_approval("refund")       # money moves require approval
    assert not billing.needs_approval("classify")
