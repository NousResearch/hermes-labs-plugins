"""Each collector against a temp HERMES_HOME seeded through core's own writers: what it
surfaces, what it filters, and that a missing or corrupt store degrades to a note."""

from __future__ import annotations

import os
import time

from inbox_plugin import collectors as c
from inbox_seed import (
    DEAD_PID, acquire_lease, add_delegations, add_kanban_tasks, make_state_db, own_start_time,
    write_cron, write_leases, write_processes,
)


class TestProcesses:
    def test_missing_file_is_noted_absent(self, hermes_home):
        assert c.collect_processes(hermes_home) == ([], "absent")

    def test_corrupt_file_is_noted_unreadable(self, hermes_home):
        (hermes_home / "processes.json").write_text("{not json", encoding="utf-8")
        items, note = c.collect_processes(hermes_home)
        assert items == [] and note.startswith("unreadable")

    def test_live_dead_and_sandboxed_pids(self, hermes_home):
        now = time.time()
        write_processes(hermes_home, [
            {"session_id": "live", "command": "sleep 5", "pid": os.getpid(), "pid_scope": "host",
             "started_at": now - 60},
            {"session_id": "dead", "command": "gone", "pid": DEAD_PID, "pid_scope": "host",
             "started_at": now - 60},
            {"session_id": "sandboxed", "command": "in docker", "pid": 42, "pid_scope": "env",
             "started_at": now - 60},
        ])
        items, note = c.collect_processes(hermes_home)
        procs = {p["id"]: p for p in items}
        assert note is None
        assert procs["live"]["alive"] is True
        assert procs["dead"]["alive"] is False
        # A PID inside a sandbox cannot be probed from the host: unknown, not dead.
        assert procs["sandboxed"]["alive"] is None

    def test_recycled_pid_is_detected_through_the_start_time(self, hermes_home):
        """A checkpointed PID the OS has since reused is not the old process. The start
        time in the entry is what tells them apart — the guard sessions already use."""
        write_processes(hermes_home, [
            {"session_id": "recycled", "command": "old job", "pid": os.getpid(),
             "pid_scope": "host", "process_start_time": own_start_time() - 5000.0},
            {"session_id": "same", "command": "real job", "pid": os.getpid(),
             "pid_scope": "host", "process_start_time": own_start_time()},
        ])
        items, note = c.collect_processes(hermes_home)
        procs = {p["id"]: p for p in items}
        assert note is None
        assert procs["recycled"]["alive"] is False
        assert procs["same"]["alive"] is True


class TestDelegations:
    def test_missing_db_is_noted_absent(self, hermes_home):
        assert c.collect_delegations(hermes_home) == ([], "absent")

    def test_running_pending_and_recent_dropped_surface(self, hermes_home):
        now = time.time()
        add_delegations(hermes_home, [
            ("run", "running", now - 60, None, "pending", 0, '{"goal": "active work"}'),
            ("done", "completed", now - 600, now - 30, "pending", 0, '{"goal": "finished work"}'),
            ("dropped_new", "stalled", now - 7200, now - 3600, "dropped", 5, '{"goal": "recent"}'),
            # Dropped a month ago: history, not inbox material.
            ("dropped_old", "stalled", now - 30 * 86400, now - 30 * 86400, "dropped", 5, "{}"),
            # Already delivered to its conversation.
            ("delivered", "completed", now - 600, now - 500, "delivered", 1, "{}"),
        ])
        items, note = c.collect_delegations(hermes_home)
        assert note is None
        assert {d["id"] for d in items} == {"run", "done", "dropped_new"}
        assert items[0]["id"] == "run"  # live work sorts first

    def test_goal_extracted_from_task_json(self, hermes_home):
        add_delegations(hermes_home, [
            ("run", "running", time.time(), None, "pending", 0, '{"goal": "research pricing"}'),
        ])
        (item,), _ = c.collect_delegations(hermes_home)
        assert item["goal"] == "research pricing"

    def test_corrupt_db_is_noted_unreadable(self, hermes_home):
        (hermes_home / "state.db").write_bytes(b"definitely not sqlite" * 100)
        items, note = c.collect_delegations(hermes_home)
        assert items == [] and note.startswith("unreadable")


class TestCron:
    def test_failed_paused_and_healthy_jobs(self, hermes_home):
        write_cron(hermes_home, [
            {"id": "j1", "name": "digest", "enabled": True, "state": "scheduled",
             "last_status": "ok", "next_run_at": "2026-10-22T09:00:00"},
            {"id": "j2", "name": "backup", "enabled": True, "state": "scheduled",
             "last_status": "error", "last_error": "disk full"},
            {"id": "j3", "name": "report", "enabled": True, "state": "scheduled",
             "last_status": "delivery_failed"},
            {"id": "j4", "name": "old", "enabled": True, "state": "paused",
             "paused_reason": "3 consecutive failures"},
        ])
        items, note = c.collect_cron(hermes_home)
        jobs = {j["id"]: j for j in items}
        assert note is None
        assert jobs["j1"]["needs_attention"] is False and jobs["j1"]["paused"] is False
        assert jobs["j2"]["needs_attention"] is True and jobs["j2"]["last_error"] == "disk full"
        assert jobs["j3"]["needs_attention"] is True
        assert jobs["j4"]["paused"] is True

    def test_bom_prefixed_file_reads(self, hermes_home):
        (hermes_home / "cron").mkdir()
        (hermes_home / "cron" / "jobs.json").write_text(
            '\ufeff{"jobs": [{"id": "a", "name": "bom"}]}', encoding="utf-8")
        items, note = c.collect_cron(hermes_home)
        assert note is None and items[0]["name"] == "bom"


class TestSessions:
    def test_dead_and_recycled_pid_leases_excluded(self, hermes_home):
        start = own_start_time()
        write_leases(hermes_home, [
            {"lease_id": "a", "session_id": "s1", "surface": "cli", "pid": os.getpid(),
             "process_start_time": start, "started_at": time.time()},
            {"lease_id": "b", "session_id": "s2", "surface": "telegram", "pid": DEAD_PID,
             "started_at": time.time()},
            # Our PID but another process's start time: the PID was recycled.
            {"lease_id": "c", "session_id": "s3", "surface": "tui", "pid": os.getpid(),
             "process_start_time": start - 1000, "started_at": time.time()},
        ])
        items, note = c.collect_sessions(hermes_home)
        assert note is None
        assert [s["session_id"] for s in items] == ["s1"]

    def test_lease_written_by_core_registry_with_title_from_session_db(self, hermes_home):
        from hermes_cli.active_sessions import release_active_session

        db = make_state_db(hermes_home)
        db.create_session("s1", "cli")
        db.set_session_title("s1", "Plan the offsite")
        db.close()
        lease = acquire_lease("s1", "cli")
        try:
            (item,), note = c.collect_sessions(hermes_home)
        finally:
            release_active_session(lease)
        assert note is None
        assert (item["session_id"], item["surface"], item["pid"]) == ("s1", "cli", os.getpid())
        assert item["title"] == "Plan the offsite"
        assert c.collect_sessions(hermes_home)[0] == []  # released: no longer open

    def test_legacy_bare_list_registry(self, hermes_home):
        write_leases(hermes_home, [{"lease_id": "a", "session_id": "s1", "surface": "cli",
                                    "pid": os.getpid(), "started_at": time.time()}], legacy=True)
        (item,), _ = c.collect_sessions(hermes_home)
        assert item["session_id"] == "s1" and item["title"] is None


class TestKanban:
    def test_missing_board_is_noted_absent(self, hermes_home):
        assert c.collect_kanban() == ([], "absent")

    def test_running_blocked_and_review_only(self, hermes_home):
        add_kanban_tasks([
            ("t_run", "Ship it", "running"),
            ("t_blk", "Needs a key", "blocked"),
            ("t_rev", "Check the PR", "review"),
            ("t_todo", "Later", "todo"),
            ("t_done", "Finished", "done"),
        ])
        items, note = c.collect_kanban()
        assert note is None
        assert [t["id"] for t in items] == ["t_blk", "t_rev", "t_run"]


class TestKanbanBoardResolution:
    """The plugin resolves the board from the documented layout instead of importing core's
    resolver; these pin it to the same answer as core's own ``kanban_db_path``."""

    @staticmethod
    def _core_path():
        from hermes_cli.kanban_db import kanban_db_path

        return kanban_db_path()

    def test_default_board(self, hermes_home):
        assert c.kanban_db_location() == self._core_path() == hermes_home / "kanban.db"

    def test_current_file_names_a_board(self, hermes_home):
        from hermes_cli.kanban_db import set_current_board
        from hermes_cli.kanban_db_boards import create_board

        create_board("proj")
        set_current_board("proj")
        assert c.kanban_db_location() == self._core_path() != hermes_home / "kanban.db"

    def test_env_board_and_missing_board(self, hermes_home, monkeypatch):
        from hermes_cli.kanban_db_boards import create_board

        create_board("proj")
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "proj")
        assert c.kanban_db_location() == self._core_path() != hermes_home / "kanban.db"
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "nope")
        assert c.kanban_db_location() == self._core_path() == hermes_home / "kanban.db"

    def test_db_pin_wins(self, hermes_home, monkeypatch):
        monkeypatch.setenv("HERMES_KANBAN_DB", str(hermes_home / "pinned.db"))
        assert c.kanban_db_location() == self._core_path() == hermes_home / "pinned.db"

    def test_tasks_read_from_the_current_board(self, hermes_home):
        from hermes_cli.kanban_db import set_current_board
        from hermes_cli.kanban_db_boards import create_board

        add_kanban_tasks([("t_default", "On default", "running")])
        create_board("proj")
        set_current_board("proj")
        add_kanban_tasks([("t_proj", "On proj", "blocked")])
        items, _ = c.collect_kanban()
        assert [t["id"] for t in items] == ["t_proj"]
