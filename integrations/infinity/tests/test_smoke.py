from conftest import drive


def test_read_roundtrip(runtime_factory):
    env, srv, rt = runtime_factory()
    st, toolset = rt.discover()
    assert st == 200 and {t["name"] for t in toolset["tools"]} >= {"lookup_lead", "update_lead", "send_followup"}
    code, cid = rt.invoke("lookup_lead", {"lead_id": "lead_dana01"})
    assert code == 200
    drive(env)
    res = rt.await_result("thread-root", cid, 5)
    assert res and "completed" in res["text"], res


def test_write_roundtrip(runtime_factory):
    env, srv, rt = runtime_factory()
    code, cid = rt.invoke("update_lead", {"lead_id": "lead_dana01", "status": "qualified"})
    assert code == 200
    drive(env)
    res = rt.await_result("thread-root", cid, 5)
    assert res and '"status":"completed"' in res["text"] and "receipt_ref" in res["text"], res
    assert env.crm.effect_count == 1
