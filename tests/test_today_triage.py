"""Batch triage (reversible only, with undo) and notifications derived from Today (agentic_os.today)."""
import pytest
from fastapi.testclient import TestClient

from agentic_os.projects_api import RegisteredProducersProvider, SampleProjectionProvider, create_app
from agentic_os.today import MAX_BATCH, Today, TodayError, stable_id

DAY = 86400.0


class Clock:
    def __init__(self, t=1_800_000_000.0):     # 2027-01-15 08:00 UTC
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def today(clock):
    return Today(clock=clock)


def cid(today, key):
    return stable_id(today.tenant, today.project, "case:" + key)


QUOTE, CLINIC, TRAVEL, INVOICE = "quote-acme-followup", "clinic-unconfirmed", "travel-taylor-visa", "invoice-1042-reminder"


def state(today, key):
    return today.item(cid(today, key))["state"]


# ── batch: reversible actions ────────────────────────────────────────────────────────────────────
def test_batch_snooze_moves_many_items_out_of_the_decision_queue(today):
    ids = [cid(today, QUOTE), cid(today, CLINIC)]
    out = today.batch("snooze", ids, seconds=3600)
    assert out["ok_count"] == 2 and out["failed_count"] == 0 and out["undo"]["token"]
    assert state(today, QUOTE) == state(today, CLINIC) == "scheduled"
    assert state(today, TRAVEL) == "needs_action"                      # untouched


def test_batch_dismiss_assign_and_categorize(today):
    ids = [cid(today, QUOTE), cid(today, CLINIC)]
    today.batch("assign", ids, assignee="Front desk")
    today.batch("categorize", ids, category="Follow-ups")
    v = today.item(ids[0])
    assert v["owner"] == "Front desk" and v["category"] == "Follow-ups"
    today.batch("dismiss", [ids[0]])
    assert state(today, QUOTE) == "dismissed"


def test_batch_never_approves_or_sends(today):
    for bad in ("approve", "send", "approve_all", ""):
        with pytest.raises(TodayError) as e:
            today.batch(bad, [cid(today, TRAVEL)])
        assert e.value.status == 422 and e.value.code == "batch_not_allowed"
    assert today._cases[cid(today, TRAVEL)].sends == 0


def test_batch_reports_each_item_and_does_not_let_one_failure_block_the_rest(today):
    iid = cid(today, TRAVEL)
    today.approve(iid, expected_digest=today.item(iid)["action"]["payload_digest"])      # now SENT
    out = today.batch("dismiss", [iid, cid(today, QUOTE), "att_doesnotexist"])
    by = {r["id"]: r for r in out["results"]}
    assert by[iid]["ok"] is False and by[iid]["error"] == "already_sent"                # a sent item can't be dismissed
    assert by[cid(today, QUOTE)]["ok"] is True
    assert by["att_doesnotexist"]["error"] == "not_changeable"
    assert out["ok_count"] == 1 and out["failed_count"] == 2
    assert state(today, TRAVEL) == "waiting"                                           # the sent item is unchanged


def test_batch_validation(today):
    ids = [cid(today, QUOTE)]
    cases = [("snooze", dict(seconds=5), "bad_snooze"), ("snooze", dict(), "bad_snooze"),
             ("assign", dict(assignee="  "), "bad_label"), ("assign", dict(assignee="x" * 61), "bad_label"),
             ("categorize", dict(category="a\nb"), "bad_label"), ("categorize", dict(category="x" * 41), "bad_label")]
    for action, kw, code in cases:
        with pytest.raises(TodayError) as e:
            today.batch(action, ids, **kw)
        assert e.value.code == code, (action, kw)
    with pytest.raises(TodayError) as e:
        today.batch("dismiss", [])
    assert e.value.code == "empty_batch"
    with pytest.raises(TodayError) as e:
        today.batch("dismiss", [f"att_{i}" for i in range(MAX_BATCH + 1)])
    assert e.value.code == "too_many"


def test_duplicate_ids_in_a_batch_are_applied_once(today):
    out = today.batch("snooze", [cid(today, QUOTE)] * 3, seconds=3600)
    assert len(out["results"]) == 1


# ── undo ─────────────────────────────────────────────────────────────────────────────────────────
def test_undo_restores_exactly_what_the_batch_changed(today):
    ids = [cid(today, QUOTE), cid(today, CLINIC)]
    out = today.batch("dismiss", ids)
    assert state(today, QUOTE) == "dismissed"
    r = today.undo(out["undo"]["token"])
    assert r["restored_count"] == 2
    assert state(today, QUOTE) == state(today, CLINIC) == "needs_action"


def test_undo_is_single_use_and_expires(today, clock):
    out = today.batch("snooze", [cid(today, QUOTE)], seconds=3600)
    today.undo(out["undo"]["token"])
    with pytest.raises(TodayError) as e:
        today.undo(out["undo"]["token"])
    assert e.value.code == "undo_unavailable"
    out2 = today.batch("snooze", [cid(today, QUOTE)], seconds=3600)
    clock.t += 601
    with pytest.raises(TodayError):
        today.undo(out2["undo"]["token"])
    with pytest.raises(TodayError):
        today.undo("undo_made_up")


def test_undo_never_overwrites_a_later_change(today):
    iid = cid(today, QUOTE)
    out = today.batch("snooze", [iid], seconds=3600)
    today.snooze(iid, seconds=7200)                                                    # the user changed it afterwards
    r = today.undo(out["undo"]["token"])
    assert r["restored_count"] == 0 and r["results"][0]["reason"] == "changed_since"
    assert state(today, QUOTE) == "scheduled"                                          # their later choice stands


def test_undo_of_a_dismiss_does_not_resurrect_an_item_that_moved_on(today):
    iid = cid(today, INVOICE)
    out = today.batch("assign", [iid], assignee="Sam")
    today._cases[iid].owner = "Someone else"
    assert today.undo(out["undo"]["token"])["restored_count"] == 0


def test_reminders_can_be_batch_snoozed_and_undone(today, clock):
    v = today.intake.capture("Remind me to call John in 2 hours")
    rid = v["routing"]["item_id"]
    clock.t += 2 * 3600 + 1
    assert today.item(rid)["state"] == "needs_action"
    out = today.batch("snooze", [rid], seconds=3600)
    assert today.item(rid)["state"] == "scheduled"
    today.undo(out["undo"]["token"])
    assert today.item(rid)["state"] == "needs_action"


# ── notifications ────────────────────────────────────────────────────────────────────────────────
def test_notifications_count_only_actionable_items_with_a_content_free_summary(today):
    n = today.notifications()
    assert n["count"] == 5 and n["summary"] == "5 things need you"
    assert "Visa" not in n["summary"]                                                   # lock-screen safe
    assert {p["title"] for p in n["pending"]} >= {"Visa documents missing — Taylor trip"}


def test_ack_silences_an_item_until_its_state_changes(today, clock):
    keys = [p["key"] for p in today.notifications()["pending"]]
    today.ack_notifications(keys)
    n = today.notifications()
    assert n["count"] == 5 and n["pending"] == []                                       # still counted, no longer interrupting
    today.ack_notifications(keys)                                                       # idempotent
    assert today.notifications()["pending"] == []


def test_snoozed_item_is_silent_until_it_wakes_then_notifies_once(today, clock):
    iid = cid(today, QUOTE)
    today.ack_notifications([p["key"] for p in today.notifications()["pending"]])
    today.snooze(iid, seconds=3600)
    assert today.notifications()["count"] == 4 and today.notifications()["pending"] == []
    clock.t += 1800
    assert today.notifications()["pending"] == []                                       # still snoozed: no notification
    clock.t += 1801
    n = today.notifications()
    assert [p["id"] for p in n["pending"]] == [iid]                                     # woke: notifies once
    today.ack_notifications([p["key"] for p in n["pending"]])
    assert today.notifications()["pending"] == []


def test_waiting_item_resurfacing_notifies_but_waiting_does_not(today, clock):
    iid = cid(today, TRAVEL)
    today.ack_notifications([p["key"] for p in today.notifications()["pending"]])
    today.approve(iid, expected_digest=today.item(iid)["action"]["payload_digest"])
    assert today.notifications()["pending"] == []                                       # waiting on the customer: silent
    clock.t += 3 * DAY + 1
    n = today.notifications()
    assert [p["id"] for p in n["pending"]] == [iid] and n["pending"][0]["resurfaced"] is True


def test_a_reminder_notifies_only_when_due(today, clock):
    v = today.intake.capture("Remind me to call John in 3 hours")
    today.ack_notifications([p["key"] for p in today.notifications()["pending"]])
    assert today.notifications()["pending"] == []
    clock.t += 3 * 3600 + 1
    n = today.notifications()
    assert [p["kind"] for p in n["pending"]] == ["reminder"]


def test_quiet_hours_suppress_interruptions_but_not_the_count(today, clock):
    today.set_notification_prefs(quiet_start=7, quiet_end=9)                            # clock is 08:00 UTC
    n = today.notifications()
    assert n["quiet_now"] is True and n["pending"] == [] and n["suppressed"] == 5 and n["count"] == 5
    clock.t += 2 * 3600                                                                 # 10:00 — quiet hours over
    assert len(today.notifications()["pending"]) == 5


def test_quiet_hours_wrap_midnight_and_respect_timezone(today, clock):
    today.set_notification_prefs(quiet_start=22, quiet_end=7, tz_offset_minutes=-300)   # 08:00 UTC = 03:00 local
    assert today.notifications()["quiet_now"] is True
    today.set_notification_prefs(clear_quiet=True)
    assert today.notifications()["quiet_now"] is False


def test_disabled_notifications_never_interrupt(today):
    today.set_notification_prefs(enabled=False)
    n = today.notifications()
    assert n["pending"] == [] and n["count"] == 5


def test_prefs_and_ack_validation(today):
    for kw in (dict(quiet_start=25, quiet_end=7), dict(quiet_start=7), dict(tz_offset_minutes=5000)):
        with pytest.raises(TodayError):
            today.set_notification_prefs(**kw)
    with pytest.raises(TodayError):
        today.ack_notifications(["x"] * 101)
    with pytest.raises(TodayError):
        today.ack_notifications([123])


def test_seen_keys_are_bounded(today):
    today.ack_notifications([f"k{i}" for i in range(100)])
    for _ in range(11):
        today.ack_notifications([f"k{_}-{i}" for i in range(100)])
    assert len(today._seen) <= 1000


# ── HTTP + fail closed ───────────────────────────────────────────────────────────────────────────
@pytest.fixture
def http():
    return TestClient(create_app(SampleProjectionProvider()))


def test_http_batch_undo_roundtrip(http):
    base = "/api/projects/customer-ops"
    items = http.get(f"{base}/today").json()["items"]
    ids = [i["id"] for i in items if i["kind"] == "case"][:2]
    r = http.post(f"{base}/today/batch", json={"action": "snooze", "item_ids": ids, "seconds": 3600})
    assert r.status_code == 200 and r.json()["ok_count"] == 2
    after = {i["id"]: i["state"] for i in http.get(f"{base}/today").json()["items"]}
    assert all(after[i] == "scheduled" for i in ids)
    u = http.post(f"{base}/today/batch/undo", json={"token": r.json()["undo"]["token"]})
    assert u.json()["restored_count"] == 2
    assert http.post(f"{base}/today/batch/undo", json={"token": r.json()["undo"]["token"]}).status_code == 404
    bad = http.post(f"{base}/today/batch", json={"action": "approve", "item_ids": ids})
    assert bad.status_code == 422 and bad.json()["detail"]["error"] == "batch_not_allowed"


def test_http_notifications_route_is_not_shadowed_by_the_item_route(http):
    base = "/api/projects/customer-ops"
    n = http.get(f"{base}/today/notifications")
    assert n.status_code == 200 and "pending" in n.json() and "summary" in n.json()
    keys = [p["key"] for p in n.json()["pending"]]
    assert http.post(f"{base}/today/notifications/ack", json={"keys": keys}).json()["acknowledged"] == len(keys)
    assert http.get(f"{base}/today/notifications").json()["pending"] == []
    p = http.put(f"{base}/today/notification-prefs", json={"quiet_start": 22, "quiet_end": 7})
    assert p.status_code == 200 and p.json()["quiet_start"] == 22
    assert http.put(f"{base}/today/notification-prefs", json={"quiet_start": 99, "quiet_end": 7}).status_code == 422


def test_http_batch_sessions_are_isolated(http):
    base = "/api/projects/customer-ops"
    ids = [i["id"] for i in http.get(f"{base}/today", headers={"X-Demo-Session": "a"}).json()["items"] if i["kind"] == "case"][:1]
    http.post(f"{base}/today/batch", json={"action": "dismiss", "item_ids": ids}, headers={"X-Demo-Session": "a"})
    other = {i["id"]: i["state"] for i in http.get(f"{base}/today", headers={"X-Demo-Session": "b"}).json()["items"]}
    assert other[ids[0]] == "needs_action"


def test_batch_fails_closed_on_a_non_sample_provider():
    c = TestClient(create_app(RegisteredProducersProvider()))
    r = c.post("/api/projects/customer-ops/today/batch", json={"action": "dismiss", "item_ids": ["att_x"]})
    assert r.status_code == 501 and r.json()["detail"]["error"] == "unsupported"


def test_every_item_id_is_stable_across_requests_including_signals():
    p = SampleProjectionProvider()
    a = [(i["id"], i["user_title"]) for i in p.today("customer-ops", "s")["items"]]
    b = [(i["id"], i["user_title"]) for i in p.today("customer-ops", "s")["items"]]
    assert a == b and len(a) > 5 and any(t.startswith("thread") for _, t in a)   # incl. the support-thread signal
