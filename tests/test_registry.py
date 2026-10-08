from agentic_os import Registry

def test_catalog_loads_and_validates():
    reg = Registry.load()
    assert len(reg) >= 10
    assert "agentic-os" not in reg.names          # the OS does not list itself as a module
    billing = reg.get("agentic-billing")
    # agentic-billing is an in-monorepo app: it has a source path, not a separate GitHub repo/url.
    assert billing.path == "apps/billing"          # in-monorepo source path (dir name, not module name)
    assert billing.url == ""                       # no separate repo → no github url (use .path)
    assert billing.needs_approval("refund")        # money moves require approval
    assert not billing.needs_approval("classify")
