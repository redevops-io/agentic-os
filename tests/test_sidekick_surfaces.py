"""Chatwoot/ERPNext/Postiz surface adapters + one session across them (plan §23/§25, P9)."""
from __future__ import annotations

from agentic_os.sidekick import (
    ChatwootSurfaceAdapter, ERPNextSurfaceAdapter, InMemorySessionStore, PostizSurfaceAdapter, SurfaceRef,
)


def test_adapters_produce_typed_context_and_deeplinks():
    cw = ChatwootSurfaceAdapter(base_url="https://support.x.io", account_id="1",
                                object_type="conversation", object_id="42")
    c = cw.context()
    assert c.app_id == "chatwoot" and c.route == "/app/accounts/1/conversations/42"
    assert "support.conversation.reply" in c.native_capabilities
    assert cw.deep_link("conversation", "42") == "https://support.x.io/app/accounts/1/conversations/42"

    erp = ERPNextSurfaceAdapter(base_url="https://erp.x.io/", object_type="sales_order", object_id="SO-1")
    assert erp.context().route == "/app/sales-order/SO-1"
    assert erp.deep_link("sales_invoice", "INV-9") == "https://erp.x.io/app/sales-invoice/INV-9"

    pz = PostizSurfaceAdapter(base_url="https://social.x.io", object_type="post", object_id="p7")
    assert pz.context().route == "/launches/p7"
    assert "content.publish" in pz.context().native_capabilities


def test_artifact_links_carry_session_and_project():
    erp = ERPNextSurfaceAdapter(base_url="https://erp.x.io")
    link = erp.artifact_link("quotation", "Q-1", project_id="Q4", session_id="sk_1")
    assert link.provider == "erpnext" and link.sidekick_session_id == "sk_1" and link.project_id == "Q4"


def test_one_session_travels_across_new_surfaces():
    store = InMemorySessionStore()
    s = store.create(tenant_id="t1", project_id="expansion", surface=SurfaceRef(app_id="twenty"))
    store.switch_surface(s.session_id, SurfaceRef(app_id="chatwoot", object_type="conversation", object_id="42"))
    store.switch_surface(s.session_id, SurfaceRef(app_id="erpnext", object_type="sales_order", object_id="SO-1"))
    got = store.get(s.session_id)
    assert got.project_id == "expansion"                        # preserved across surfaces
    assert [r.app_id for r in got.recent_surfaces][-2:] == ["chatwoot", "erpnext"]
