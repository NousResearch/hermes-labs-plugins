"""The assembled snapshot: section classification, rendering, JSON shape, and that building
it writes nothing to the home it reads."""

from __future__ import annotations

import json
import os
import time

from inbox_plugin import report
from inbox_seed import (
    DEAD_PID, add_delegations, add_kanban_tasks, make_state_db, tree, write_cron, write_leases,
    write_processes,
)


def _seed_everything(home):
    now = time.time()
    write_processes(home, [
        {"session_id": "live", "command": "npm run dev", "pid": os.getpid(), "pid_scope": "host",
         "started_at": now},
        {"session_id": "dead", "command": "y", "pid": DEAD_PID, "pid_scope": "host", "started_at": now},
    ])
    add_delegations(home, [
        ("run", "running", now, None, "pending", 0, '{"goal": "scan the repo"}'),
        ("done", "completed", now - 60, now - 10, "pending", 0, "{}"),
    ])
    write_cron(home, [
        {"id": "j1", "name": "ok", "enabled": True, "last_status": "ok", "next_run_at": "soon"},
        {"id": "j2", "name": "bad", "enabled": True, "last_status": "error", "last_error": "boom"},
    ])
    write_leases(home, [{"lease_id": "l", "session_id": "s1", "surface": "cli", "pid": os.getpid(),
                         "started_at": now}])
    add_kanban_tasks([("t_blk", "Blocked card", "blocked"), ("t_run", "Running card", "running")])


def test_sections_classified(hermes_home):
    _seed_everything(hermes_home)
    data = report.build_inbox()

    assert data["home"] == str(hermes_home)
    assert {i["id"] for i in data["attention"]} == {"dead", "j2", "t_blk"}
    assert {i["id"] for i in data["in_progress"]} == {"live", "run", "t_run"}
    assert [i["id"] for i in data["finished_undelivered"]] == ["done"]
    assert [s["session_id"] for s in data["sessions"]] == ["s1"]
    assert set(data["sources"].values()) == {"read"}


def test_render_lists_every_section(hermes_home):
    _seed_everything(hermes_home)
    text = report.render_inbox(report.build_inbox())
    for needle in ("Needs attention (3)", "GONE (stale checkpoint)", "last run FAILED: boom",
                   "kanban t_blk  [blocked]", "In progress (3)", "npm run dev",
                   "Finished, result not yet delivered (1)", "(result awaiting delivery)",
                   "Scheduled (2 active job(s))", "ok  next: soon", "Open surfaces (1)"):
        assert needle in text, needle
    assert "Not read" not in text


def test_empty_home_all_clear_and_sources_named(hermes_home):
    data = report.build_inbox()
    assert data["attention"] == [] and data["in_progress"] == [] and data["finished_undelivered"] == []
    assert data["sources"] == {name: "absent" for name in report.SOURCE_LABELS}
    text = report.render_inbox(data)
    assert "nothing — all clear" in text and "nothing running in the background" in text
    assert "async delegations (state.db): absent" in text
    json.dumps(data)


def test_reading_writes_nothing(hermes_home):
    _seed_everything(hermes_home)
    before = tree(hermes_home)
    report.build_inbox()
    assert tree(hermes_home) == before


def test_empty_home_stays_empty(hermes_home):
    report.build_inbox()
    assert list(hermes_home.iterdir()) == []


def test_state_db_without_delegation_table_degrades(hermes_home):
    # A state.db from a build that predates async delegations: the section is skipped, the rest
    # of the inbox still renders.
    make_state_db(hermes_home).close()
    import sqlite3

    conn = sqlite3.connect(hermes_home / "state.db")
    conn.execute("DROP TABLE async_delegations")
    conn.commit()
    conn.close()
    data = report.build_inbox()
    assert data["sources"]["delegations"].startswith("unreadable")
    assert "Needs attention (0)" in report.render_inbox(data)
