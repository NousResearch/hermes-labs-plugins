"""Shared seeding helpers: every store is created through Hermes' own code where one exists
(SessionDB for state.db, kanban_db.init_db for kanban.db), so the plugin's queries run against
core's real schema, not a copy of it."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

# Far above any PID a test host hands out.
DEAD_PID = 2**22 + 12345


def own_start_time() -> float:
    import psutil

    return psutil.Process(os.getpid()).create_time()


def write_processes(home: Path, entries: list[dict]) -> None:
    (home / "processes.json").write_text(json.dumps(entries), encoding="utf-8")


def make_state_db(home: Path):
    from hermes_state import SessionDB

    return SessionDB(db_path=home / "state.db")


def add_delegations(home: Path, rows: list[tuple]) -> None:
    """rows: (id, state, dispatched_at, completed_at, delivery_state, attempts, task_json)."""
    make_state_db(home).close()
    conn = sqlite3.connect(home / "state.db")
    now = time.time()
    conn.executemany(
        """INSERT INTO async_delegations (delegation_id, origin_session, state, dispatched_at,
               completed_at, updated_at, delivery_state, delivery_attempts, task_json)
           VALUES (?, 'origin', ?, ?, ?, ?, ?, ?, ?)""",
        [(r[0], r[1], r[2], r[3], now, r[4], r[5], r[6]) for r in rows],
    )
    conn.commit()
    conn.close()


def write_cron(home: Path, jobs: list[dict]) -> None:
    (home / "cron").mkdir(exist_ok=True)
    (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": jobs}), encoding="utf-8")


def write_leases(home: Path, leases: list[dict], *, legacy: bool = False) -> None:
    """Hand-written leases (for dead / recycled PIDs core's writer would refuse to record), in
    the registry's current {"entries": [...]} shape, or the older bare list."""
    (home / "runtime").mkdir(exist_ok=True)
    payload = leases if legacy else {"entries": leases}
    (home / "runtime" / "active_sessions.json").write_text(json.dumps(payload), encoding="utf-8")


def acquire_lease(session_id: str, surface: str):
    """A real lease held by this test process, written by core's own registry."""
    from hermes_cli.active_sessions import try_acquire_active_session

    lease, refusal = try_acquire_active_session(session_id=session_id, surface=surface, config={})
    assert lease is not None, refusal
    return lease


def add_kanban_tasks(rows: list[tuple[str, str, str]]) -> None:
    """rows: (id, title, status) on the current board, created with core's schema."""
    from hermes_cli.kanban_db import init_db

    db = init_db()
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO tasks (id, title, status, created_at) VALUES (?, ?, ?, ?)",
        [(tid, title, status, int(time.time())) for tid, title, status in rows],
    )
    conn.commit()
    conn.close()


def tree(home: Path) -> dict[str, tuple[int, int]]:
    """Every file under home with (size, mtime_ns), for no-write assertions. SQLite's WAL
    sidecars (-wal/-shm) are left out: any reader of a WAL database, read-only included,
    creates them to coordinate with writers; they hold no data of their own here."""
    return {str(p.relative_to(home)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in sorted(home.rglob("*"))
            if p.is_file() and not p.name.endswith(("-wal", "-shm"))}
