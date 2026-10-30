"""Isolated real-SQLite acceptance for observation ingestion and consumer cursors."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
import sqlite3
import pytest
from inbox_plugin.state import InboxState, InboxStateError


def item(fp="a", state="running", identity="task:1"):
    return {"id": identity, "fingerprint": fp, "kind": "task", "state": state}


def test_reads_and_constructor_are_nonmutating(tmp_path):
    store = InboxState(tmp_path, "alex")
    assert store.changes()["events"] == []
    assert not (tmp_path / "inbox").exists()
    assert store.observe([]) == []
    assert store.mark_seen() == 0
    assert not (tmp_path / "inbox").exists()


def test_identical_snapshot_and_repeated_transition(tmp_path):
    store = InboxState(tmp_path, "alex")
    first = store.observe([item()])
    assert len(first) == 1 and store.observe([item()]) == []
    second = store.observe([item("b", "completed")])
    third = store.observe([item()])
    assert first[0] < second[0] < third[0]
    assert [e["fingerprint"] for e in store.changes()["events"]] == ["a", "b", "a"]


def test_independent_consumers_restart_and_monotonic_cursors(tmp_path):
    store = InboxState(tmp_path, "alex")
    first = store.observe([item()])[0]
    second = store.observe([item("b")])[0]
    assert store.changes()["cursor"] == 0
    assert store.changes()["cursor"] == 0
    assert store.mark_seen("phone", first) == first
    assert store.changes("phone")["events"][0]["seq"] == second
    assert store.changes("owner")["events"][0]["seq"] == first
    reopened = InboxState(tmp_path, "alex")
    assert reopened.mark_seen("phone", second) == second
    assert reopened.mark_seen("phone", first) == second
    assert reopened.changes("phone")["events"] == []
    with pytest.raises(InboxStateError):
        reopened.mark_seen("phone", second + 10)
    assert reopened.changes("phone")["cursor"] == second


def test_profiles_share_no_events_or_cursors(tmp_path):
    a, b = InboxState(tmp_path, "alex"), InboxState(tmp_path, "sam")
    a.observe([item()]); b.observe([item()])
    a.mark_seen()
    assert a.changes()["events"] == []
    assert len(b.changes()["events"]) == 1


def test_truncation_and_latest_are_not_the_saved_cursor(tmp_path):
    store = InboxState(tmp_path, "alex")
    seqs = store.observe([item(str(i), identity=f"task:{i}") for i in range(5)])
    page = store.changes(limit=2)
    assert len(page["events"]) == 2 and page["truncated"] is True
    assert page["cursor"] == 0 and page["latest"] == seqs[-1]
    store.mark_seen(upto=page["events"][-1]["seq"])
    assert len(store.changes()["events"]) == 3


def test_full_batch_validation_and_unknown_fields_not_persisted(tmp_path):
    store = InboxState(tmp_path, "alex")
    with pytest.raises(InboxStateError):
        store.observe([item(), {"id": "invalid"}])
    assert not (tmp_path / "inbox").exists()
    store.observe([dict(item(), message="must not persist", api_key="never store")])
    assert "must not persist" not in Path(store.db_path).read_bytes().decode(errors="ignore")


def test_corrupt_store_is_an_error_not_empty(tmp_path):
    store = InboxState(tmp_path, "alex")
    Path(store.db_path).parent.mkdir()
    Path(store.db_path).write_bytes(b"not sqlite")
    with pytest.raises(InboxStateError):
        store.changes()


def test_fresh_database_four_concurrent_callers(tmp_path):
    for trial in range(10):
        home = tmp_path / str(trial)
        barrier = Barrier(4)
        def observe(_):
            barrier.wait(timeout=5)
            return InboxState(home, "alex").observe([item()])
        with ThreadPoolExecutor(max_workers=4) as pool:
            receipts = list(pool.map(observe, range(4)))
        assert sum(map(len, receipts)) == 1
        assert len(InboxState(home, "alex").changes()["events"]) == 1


def test_path_with_sqlite_uri_punctuation(tmp_path):
    # '?' is not a legal Windows filename; '#' and '%' exercise URI quoting there.
    import os
    scope = "scope%#fragment" if os.name == "nt" else "scope?%#fragment"
    store = InboxState(tmp_path / scope, "alex")
    store.observe([item()])
    assert len(store.changes()["events"]) == 1


@pytest.mark.parametrize("component", ["file", "directory"])
def test_local_writes_refuse_a_symlink_to_source_data(tmp_path, component):
    store = InboxState(tmp_path, "alex")
    target = tmp_path / "native-source"
    if component == "file":
        target.write_bytes(b"native-data-unchanged")
        store.db_path.parent.mkdir()
        link = store.db_path
    else:
        target.mkdir()
        link = store.db_path.parent
    try:
        link.symlink_to(target, target_is_directory=component == "directory")
    except OSError:
        pytest.skip("symlink creation is not permitted on this host")
    with pytest.raises(InboxStateError):
        store.observe([item()])
    if component == "file":
        assert target.read_bytes() == b"native-data-unchanged"
    else:
        assert list(target.iterdir()) == []


@pytest.mark.parametrize("consumer", ["../other", "", "a" * 65, None])
def test_invalid_consumer_is_rejected_without_creation(tmp_path, consumer):
    store = InboxState(tmp_path, "alex")
    with pytest.raises(InboxStateError):
        store.changes(consumer)
    assert not (tmp_path / "inbox").exists()
