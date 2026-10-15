"""Capability model: declared AND proven; fail closed with typed errors; config parsing."""
from __future__ import annotations

import pytest


def decl(mods, provider="onlyoffice", caps=None, **kw):
    return mods.backend.BackendDecl.parse({"provider": provider, "deployment": "external",
                                           "capabilities": caps or {}, **kw})


ALL = {c: True for c in ("open", "convert", "inspect", "tracked_changes", "automated_edit", "export")}


def test_config_block_roundtrip(mods):
    d = decl(mods, caps={"open": True, "convert": True, "tracked_changes": False})
    assert d.provider == "onlyoffice" and d.deployment == "external" and d.capabilities == {"open", "convert"}


@pytest.mark.parametrize("raw", [None, {}, {"provider": "libreoffice"}, {"provider": "onlyoffice", "deployment": "bundled"},
                                 {"provider": "onlyoffice", "deployment": "managed"},
                                 {"provider": "onlyoffice", "capabilities": {"teleport": True}},
                                 {"provider": "onlyoffice", "capabilities": {"open": "yes"}},
                                 {"provider": "onlyoffice", "capabilities": [1]},
                                 {"provider": "onlyoffice", "extra": 1}])
def test_invalid_blocks_fail_closed(mods, raw):
    with pytest.raises(mods.errors.ConfigError):
        mods.backend.BackendDecl.parse(raw)


def test_elixiroffice_and_collabora_are_declarable_but_have_no_adapter(mods):
    for prov in ("collabora", "elixiroffice"):
        be = mods.backend.UnimplementedBackend(decl(mods, prov, ALL, experimental_enabled=True))
        rep = be.describe().report()
        assert not any(rep["capabilities"].values()) and rep["enabled"] is False
        for op in (lambda: be.inspect(None), lambda: be.convert(None, "pdf"), lambda: be.export(None, "pdf"),
                   lambda: be.apply_edit(None, [], track_changes=False, idem_key="x"),
                   lambda: be.open_session(None, user_id="u", ttl_s=60)):
            with pytest.raises(mods.errors.UnsupportedCapability):
                op()


def test_effective_is_declared_and_proven(mods, fake_backend_cls):
    class Partial(fake_backend_cls):
        PROVEN = frozenset({"convert", "export"})
    be = Partial(decl(mods, caps=ALL))
    rep = be.describe().report()["capabilities"]
    assert rep == {"open": False, "convert": True, "inspect": False, "tracked_changes": False,
                   "automated_edit": False, "export": True}


def test_proven_but_not_declared_is_off(mods, fake_backend_cls):
    be = fake_backend_cls(decl(mods, caps={"convert": True}))
    assert be.describe().report()["capabilities"]["automated_edit"] is False
    with pytest.raises(mods.errors.UnsupportedCapability, match="not declared"):
        be.apply_edit(None, [], track_changes=False, idem_key="k")


def test_declared_but_unproven_fails_closed_with_typed_error(mods, fake_backend_cls):
    class NoEdit(fake_backend_cls):
        PROVEN = frozenset({"convert"})
    be = NoEdit(decl(mods, caps=ALL))
    with pytest.raises(mods.errors.UnsupportedCapability, match="not proven") as ei:
        be.apply_edit(None, [], track_changes=False, idem_key="k")
    assert ei.value.to_result()["status"] == "denied" and ei.value.to_result()["executed"] is False
    assert be.calls == []                                                # no partial work


def test_tracked_changes_requires_its_own_capability(mods, fake_backend_cls):
    be = fake_backend_cls(decl(mods, caps={**ALL, "tracked_changes": False}))
    with pytest.raises(mods.errors.UnsupportedCapability, match="tracked_changes"):
        be.apply_edit(None, [], track_changes=True, idem_key="k")
    assert be.calls == []


def test_every_operation_checks_its_capability_first(mods, fake_backend_cls):
    be = fake_backend_cls(decl(mods, caps={}))
    for op in (lambda: be.inspect(None), lambda: be.convert(None, "pdf"), lambda: be.export(None, "pdf"),
               lambda: be.open_session(None, user_id="u", ttl_s=1), lambda: be.apply_edit(None, [], track_changes=False, idem_key="k")):
        with pytest.raises(mods.errors.UnsupportedCapability):
            op()
    assert be.calls == []
    assert be.health()["ok"]                                              # health is not a capability


def test_unknown_capability_name_is_refused(mods, fake_backend_cls):
    with pytest.raises(mods.errors.UnsupportedCapability):
        fake_backend_cls(decl(mods, caps=ALL)).require("teleport")
