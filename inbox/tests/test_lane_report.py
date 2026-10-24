"""Native-schema integration gates for lanes and local-only suppression."""
import time

from inbox_plugin import report
from inbox_plugin.observations import records
from inbox_seed import add_delegations, write_cron


def test_delivered_completed_native_work_is_not_lost(hermes_home):
    now = time.time()
    add_delegations(hermes_home, [("closed", "completed", now - 20, now, "delivered", 1, "{}")])
    snapshot = report.build_inbox(home=hermes_home)
    assert snapshot["finished_undelivered"] == []
    assert any(i["id"] == "closed" for i in snapshot["lanes"]["Unknown"])
    event = next(i for i in records(snapshot) if i["id"] == "delegation:closed")
    assert event["state"] == "completed"
    assert not snapshot["lanes"]["Done"]


def test_hidden_failure_still_has_raw_source_fact_and_never_all_clear(hermes_home, monkeypatch):
    from inbox_plugin.attention import AttentionState
    from inbox_plugin.state import InboxState
    monkeypatch.setattr(report.herald, "profile_identity", lambda home: "operator")
    write_cron(hermes_home, [{"id": "failure", "name": "Fixture failure", "enabled": True,
                             "last_status": "error", "last_error": "fixture only"}])
    snapshot = report.build_inbox(home=hermes_home)
    state = InboxState(hermes_home, "operator")
    state.observe(records(snapshot))
    event = next(i for i in state.changes()["events"] if i["item_id"] == "cron:failure")
    AttentionState(state).ack(event["seq"])
    after = report.build_inbox(home=hermes_home)
    assert after["attention"][0]["last_status"] == "error"
    assert after["visible_attention"] == []
    text = report.render_inbox(after)
    assert "hidden locally; source state unchanged" in text
    assert "all clear" not in text
    assert "Unknown (0 visible / 1 recorded)" in text


def test_native_limit_is_disclosed(hermes_home):
    now = time.time()
    add_delegations(hermes_home, [(f"run-{index}", "running", now, None, "pending", 0, "{}")
                                  for index in range(21)])
    snapshot = report.build_inbox(home=hermes_home)
    assert snapshot["native_limits"]["delegations"]["truncated"] is True
    assert len(snapshot["native"]) == 20
    assert "truncated" in snapshot["sources"]["delegations"]
    assert "all clear" not in report.render_inbox(snapshot)


def test_bad_local_state_discloses_failure_instead_of_hiding_work(hermes_home, monkeypatch):
    monkeypatch.setattr(report.herald, "profile_identity", lambda home: "operator")
    owned = hermes_home / "inbox"
    owned.mkdir()
    (owned / "state.db").write_bytes(b"not a database")
    snapshot = report.build_inbox(home=hermes_home)
    assert snapshot["attention_state_status"] == "unreadable"
    assert "Local attention state unreadable; no items suppressed" in report.render_inbox(snapshot)
