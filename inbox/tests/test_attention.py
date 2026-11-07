"""Tests for AttentionState ack/snooze lanes over durable observations.

All tests use a unique ``tmp_path`` per test and import
``AttentionState`` from ``inbox_plugin.attention`` — never from the
``inbox`` package directly.
"""
from __future__ import annotations

import sqlite3
import pytest

from inbox_plugin.state import InboxState, InboxStateError
from inbox_plugin.attention import AttentionState


def _tables(path):  # noqa: E741 – temporary helper, local to this module
    """Return the set of table names in the SQLite database at *path*."""
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def item(fp="a", state="failed", identity="task:1"):
    return {"id": identity, "fingerprint": fp, "kind": "task", "state": state}


@pytest.fixture
def store(tmp_path):
    return InboxState(tmp_path, "alex")


@pytest.fixture
def att(store):
    return AttentionState(store)


# ---------------------------------------------------------------------------
# 1. Missing local state → all items visible, occurrence None, no DB created
# ---------------------------------------------------------------------------
def test_read_missing_db_all_visible_no_creation(store, att, tmp_path):
    items = [item("a", "failed", identity="task:1"), item("b", "completed", identity="task:2")]
    state = att.states_for(items, now=1.0)
    assert state == {
        "task:1": {"occurrence": None, "acknowledged": False,
                    "snooze_until": None, "visible": True},
        "task:2": {"occurrence": None, "acknowledged": False,
                    "snooze_until": None, "visible": True},
    }
    assert not (tmp_path / "inbox").exists()


# ---------------------------------------------------------------------------
# 2. Existing DB with no attention table → unack defaults, latest seq resolved
# ---------------------------------------------------------------------------
def test_read_no_attention_table_resolves_latest(store, att):
    seq = store.observe([item("a", "failed")])[0]
    out = att.states_for([item("a", "failed")], now=1.0)
    assert out["task:1"] == {"occurrence": seq, "acknowledged": False,
                              "snooze_until": None, "visible": True}
    # No attention table was created
    assert "attention" not in _tables(store.db_path)


# ---------------------------------------------------------------------------
# 3. Ack repeated is idempotent and clears any snooze
# ---------------------------------------------------------------------------
def test_ack_idempotent_clears_snooze(store, att):
    seq = store.observe([item("a", "failed")])[0]
    # snooze first, then ack
    att.snooze(seq, 60.0, now=1000.0)
    first = att.ack(seq)
    assert first == {"occurrence": seq, "acknowledged": True,
                     "snooze_until": None}
    second = att.ack(seq)
    assert second == first  # idempotent
    # After ack, visible despite old snooze deadline having passed
    out = att.states_for([item("a", "failed")], now=1500.0)
    assert out["task:1"] == {"occurrence": seq, "acknowledged": True,
                              "snooze_until": None, "visible": False}


# ---------------------------------------------------------------------------
# 4. Snooze hides the item while deadline is in the future, then becomes
#    visible again once the deadline has expired
# ---------------------------------------------------------------------------
def test_snooze_hides_then_expires(store, att):
    seq = store.observe([item("a", "failed")])[0]
    att.snooze(seq, 60.0, now=1000.0)  # deadline = 1060
    hidden = att.states_for([item("a", "failed")], now=1000.0)
    assert hidden["task:1"]["visible"] is False
    assert hidden["task:1"]["snooze_until"] == 1060.0
    expired = att.states_for([item("a", "failed")], now=1060.0)
    assert expired["task:1"]["visible"] is True
    assert expired["task:1"]["acknowledged"] is False


# ---------------------------------------------------------------------------
# 5. Invalid occurrence values are rejected (bool, 0, negative, float, str,
#    None) — and the rejection happens before any write transaction starts,
#    so the database is untouched.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("occurrence", [0, -1, True, 1.5, "x", None])
def test_invalid_occurrence_rejected(store, att, occurrence):
    # Invalid occurrences are rejected at validation time, before any write
    # transaction starts and before the attention table is created.
    with pytest.raises(InboxStateError):
        att.ack(occurrence)
    with pytest.raises(InboxStateError):
        att.snooze(occurrence, 60.0, now=1000.0)


# ---------------------------------------------------------------------------
# 6. Invalid snooze seconds: NaN, infinite, negative, zero, > one week, bool
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("seconds", [
    float("nan"), float("inf"), float("-inf"), -1.0, 0.0,
    604801, True,
])
def test_invalid_snooze_seconds_rejected(store, att, seconds):
    seq = store.observe([item("a", "failed")])[0]
    with pytest.raises(InboxStateError):
        att.snooze(seq, seconds, now=1000.0)
    # No attention table should have been created
    assert "attention" not in _tables(store.db_path)


# ---------------------------------------------------------------------------
# 7. Unknown occurrence (valid int but no matching observation) raises
# ---------------------------------------------------------------------------
def test_unknown_occurrence_raises(store, att):
    store.observe([item("a", "failed")])
    with pytest.raises(InboxStateError):
        att.ack(9999)
    with pytest.raises(InboxStateError):
        att.snooze(9999, 60.0, now=1000.0)


def test_unknown_occurrence_on_missing_state_is_nonmutating(store, att):
    with pytest.raises(InboxStateError):
        att.ack(9999)
    assert not store.db_path.exists()
    assert store.changes()["events"] == []


# ---------------------------------------------------------------------------
# 8. Foreign profile occurrence: an occurrence that belongs to another
#    profile must be refused
# ---------------------------------------------------------------------------
def test_foreign_profile_occurrence_refused(store, tmp_path):
    seq = store.observe([item("a", "failed")])[0]
    # "sam" shares the same DB file but different profile
    sam_state = InboxState(tmp_path, "sam")
    sam_att = AttentionState(sam_state)
    with pytest.raises(InboxStateError):
        sam_att.ack(seq)
    with pytest.raises(InboxStateError):
        sam_att.snooze(seq, 60.0, now=1000.0)


# ---------------------------------------------------------------------------
# 9. New occurrence after A → B → A: middle failure changes the fingerprint,
#    so the new occurrence is independent and unacked even though the first
#    and last have the same fingerprint/state
# ---------------------------------------------------------------------------
def test_new_occurrence_after_a_to_b_to_a(store, att):
    a = item("fp-a", "failed", identity="task:1")
    b = item("fp-b", "failed", identity="task:1")
    seq_a = store.observe([a])[0]
    seq_b = store.observe([b])[0]
    # Ack the first occurrence
    att.ack(seq_a)
    # Change to B — new occurrence, old ack cannot hide it
    mid = att.states_for([b], now=1.0)
    assert mid["task:1"] == {"occurrence": seq_b, "acknowledged": False,
                              "snooze_until": None, "visible": True}
    # Revert to A — another new occurrence; ack on seq_a cannot hide it
    store.observe([a])
    out = att.states_for([a], now=1.0)
    assert out["task:1"]["occurrence"] > seq_a  # a fresh seq
    assert out["task:1"]["acknowledged"] is False
    assert out["task:1"]["visible"] is True


# ---------------------------------------------------------------------------
# 10. Changed failure before/after observe: a snooze on an old occurrence
#     must not hide a new failure that arrives with a different state/fingerprint
# ---------------------------------------------------------------------------
def test_changed_failure_visibility_before_and_after_observe(store, att):
    failed = item("fp-a", "failed")
    completed = {"id": "task:1", "fingerprint": "fp-b", "kind": "task", "state": "completed"}
    seq = store.observe([failed])[0]
    att.snooze(seq, 60.0, now=1000.0)  # deadline 1060
    # A different, unobserved change must surface despite the snooze
    before = att.states_for([completed], now=1099.0)
    assert before["task:1"] == {"occurrence": None, "acknowledged": False,
                                 "snooze_until": None, "visible": True}
    # Now observe the new state — a fresh occurrence, not hidden by the old snooze
    seq2 = store.observe([completed])[0]
    after = att.states_for([completed], now=1099.0)
    assert after["task:1"] == {"occurrence": seq2, "acknowledged": False,
                                "snooze_until": None, "visible": True}


# ---------------------------------------------------------------------------
# 11. Source observations are never mutated or have raw results copied into
#     the attention store; they remain the single source of truth.
# ---------------------------------------------------------------------------
def test_source_observations_not_mutated(store, att):
    seq = store.observe([item("a", "failed")])[0]
    att.ack(seq)
    # Access source rows through a readonly connection
    with store._connection() as conn:
        obs = conn.execute(
            "SELECT item_id, fingerprint, kind, state FROM observations "
            "WHERE profile=? AND seq=?", (store.profile, seq)).fetchone()
        latest = conn.execute(
            "SELECT item_id, fingerprint, kind, state, seq FROM latest "
            "WHERE profile=? AND item_id=?",
            (store.profile, "task:1")).fetchone()
    assert tuple(obs) == ("task:1", "a", "task", "failed")
    assert tuple(latest) == ("task:1", "a", "task", "failed", seq)
    # The attention table has only the four columns we defined
    with store._connection() as conn:
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(attention)")}
    assert cols == {"profile", "occurrence", "acknowledged", "snooze_until"}


# ---------------------------------------------------------------------------
# 12. Ack after snooze clears the snooze deadline; snooze preserves ack
#     history (no implicit unack)
# ---------------------------------------------------------------------------
def test_ack_after_snooze_clears_deadline(store, att):
    seq = store.observe([item("a", "failed")])[0]
    att.snooze(seq, 60.0, now=1000.0)  # deadline 1060
    # Snoozed: visible=False
    hidden = att.states_for([item("a", "failed")], now=1000.0)
    assert hidden["task:1"]["visible"] is False
    # Ack now clears snooze; item stays handled (visible=False while acked)
    acked = att.ack(seq)
    assert acked == {"occurrence": seq, "acknowledged": True,
                     "snooze_until": None}
    # After ack the old snooze deadline is cleared
    post_ack = att.states_for([item("a", "failed")], now=2000.0)
    assert post_ack["task:1"]["snooze_until"] is None
    assert post_ack["task:1"]["visible"] is False
    # Snooze again preserves the ack history (no implicit unack)
    att.snooze(seq, 30.0, now=2000.0)  # deadline 2030
    recheck = att.states_for([item("a", "failed")], now=2000.0)
    assert recheck["task:1"]["acknowledged"] is True
    assert recheck["task:1"]["snooze_until"] == 2030.0
    assert recheck["task:1"]["visible"] is False