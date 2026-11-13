"""Read-only collectors for ``hermes inbox``.

Each collector returns ``(items, note)``: ``items`` is a list of plain dicts, ``note`` is ``None``
when the source was read, else a short reason it was skipped (absent, unreadable). Nothing here
writes: JSON stores are read as files, SQLite stores are opened with ``mode=ro``, and session
titles come from ``SessionDB(read_only=True)``. Paths resolve at call time, so the active
profile's home is the one read.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

Items = list[dict[str, Any]]
Note = Optional[str]

# Delegation states the delegation tool treats as still executing.
LIVE_DELEGATION_STATES = ("running", "finalizing", "stalling")
# A dropped result older than this is history, not something to act on.
DROPPED_WINDOW_S = 7 * 86400
OK_CRON_STATUSES = (None, "", "success", "ok")
_BOARD_SLUG = re.compile(r"^[a-z0-9][a-z0-9\-_]{0,63}$")


def pid_alive(pid: Any, expected_start: Any = None) -> bool:
    """psutil liveness (no ``os.kill(pid, 0)``: on Windows that sends CTRL_C_EVENT). When the
    store recorded the process start time, a recycled PID with a different start is dead."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    import psutil

    if not psutil.pid_exists(pid):
        return False
    if not isinstance(expected_start, (int, float)) or isinstance(expected_start, bool):
        return True
    try:
        return abs(psutil.Process(pid).create_time() - float(expected_start)) < 0.001
    except psutil.Error:
        return False


def _read_json(path: Path) -> tuple[Any, Note]:
    if not path.exists():
        return None, "absent"
    try:
        # utf-8-sig: Windows editors may leave a BOM, which Hermes' own readers accept.
        return json.loads(path.read_text(encoding="utf-8-sig")), None
    except (OSError, ValueError) as exc:
        return None, f"unreadable ({type(exc).__name__})"


def _connect_ro(db: Path) -> sqlite3.Connection:
    # as_uri() quotes the path and handles Windows drive letters; a 2 s busy timeout means a
    # locked database degrades the section instead of hanging the command.
    return sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)


def collect_processes(home: Path) -> tuple[Items, Note]:
    """Background processes from the process registry checkpoint (``processes.json``). The
    checkpoint lists processes that had not exited when it was written; the owner may have died
    since, so each host PID is re-checked. PIDs inside a sandbox (``pid_scope`` other than
    ``host``) cannot be probed from here and stay ``alive: None``."""
    entries, note = _read_json(home / "processes.json")
    if note:
        return [], note
    if not isinstance(entries, list):
        return [], "unreadable (not a list)"
    now = time.time()
    out: Items = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        scope = entry.get("pid_scope", "host")
        started = entry.get("started_at")
        out.append({
            "kind": "process",
            "id": entry.get("session_id", "?"),
            "command": str(entry.get("command", ""))[:120],
            "pid": entry.get("pid"),
            "alive": (pid_alive(entry.get("pid"), entry.get("process_start_time"))
                      if scope == "host" else None),
            "age_seconds": max(0.0, now - float(started)) if isinstance(started, (int, float)) else None,
            "notify_on_complete": bool(entry.get("notify_on_complete")),
            "cwd": entry.get("cwd"),
        })
    return out, None


def collect_delegations(home: Path, limit: int = 20) -> tuple[Items, Note]:
    """Async delegations from ``state.db``: running ones, finished ones whose result has not
    reached its conversation yet, and results dropped after failed deliveries in the last week."""
    db = home / "state.db"
    if not db.exists():
        return [], "absent"
    # The live states are a fixed tuple, but they travel as bound parameters like
    # everything else: one interpolation bug never gets a chance to exist.
    live = ",".join("?" for _ in LIVE_DELEGATION_STATES)
    try:
        conn = _connect_ro(db)
        try:
            rows = conn.execute(
                f"""SELECT delegation_id, state, dispatched_at, completed_at,
                           delivery_state, delivery_attempts, task_json
                    FROM async_delegations
                    WHERE state IN ({live})
                       OR delivery_state = 'pending'
                       OR (delivery_state = 'dropped' AND COALESCE(completed_at, dispatched_at) >= ?)
                    ORDER BY CASE WHEN state IN ({live}) THEN 0 ELSE 1 END,
                             COALESCE(completed_at, dispatched_at) DESC
                    LIMIT ?""",
                (*LIVE_DELEGATION_STATES, time.time() - DROPPED_WINDOW_S,
                 *LIVE_DELEGATION_STATES, limit),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        # No async_delegations table yet (delegation never used) lands here too.
        return [], f"unreadable ({exc})"
    out: Items = []
    for delegation_id, state, dispatched_at, completed_at, delivery_state, attempts, task_json in rows:
        try:
            task = json.loads(task_json or "{}")
        except ValueError:
            task = {}
        out.append({
            "kind": "delegation",
            "id": delegation_id,
            "state": state,
            "goal": str(task.get("goal") or "")[:100] if isinstance(task, dict) else "",
            "dispatched_at": dispatched_at,
            "completed_at": completed_at,
            "delivery_state": delivery_state,
            "delivery_attempts": attempts,
        })
    return out, None


def collect_cron(home: Path) -> tuple[Items, Note]:
    """Cron jobs from ``cron/jobs.json``: failed or paused jobs and the next runs."""
    data, note = _read_json(home / "cron" / "jobs.json")
    if note:
        return [], note
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        return [], "unreadable (unexpected shape)"
    out: Items = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        enabled = bool(job.get("enabled", True))
        last_error = job.get("last_error")
        out.append({
            "kind": "cron",
            "id": job.get("id", "?"),
            "name": job.get("name") or job.get("id") or "?",
            "enabled": enabled,
            "paused": job.get("state") == "paused" or not enabled,
            "paused_reason": job.get("paused_reason"),
            "last_status": job.get("last_status"),
            # Script failures carry multi-line stderr; one line keeps the view readable.
            "last_error": " ".join(str(last_error).split())[:160] if last_error else None,
            "last_run_at": job.get("last_run_at"),
            "next_run_at": job.get("next_run_at"),
            "needs_attention": bool(last_error) or job.get("last_status") not in OK_CRON_STATUSES,
        })
    return out, None


def _session_titles(home: Path, session_ids: list[str]) -> dict[str, str]:
    """Titles through the public read-only SessionDB attach. Titles only decorate the lines, so
    any failure to open state.db (absent, locked, older schema) just leaves the ids."""
    db = home / "state.db"
    if not session_ids or not db.exists():
        return {}
    try:
        from hermes_state import SessionDB

        sdb = SessionDB(db_path=db, read_only=True)
    except Exception:
        return {}
    try:
        titles = {}
        for sid in session_ids:
            try:
                title = sdb.get_session_title(sid)
            except Exception:
                continue
            if title:
                titles[sid] = title
        return titles
    finally:
        sdb.close()


def collect_sessions(home: Path) -> tuple[Items, Note]:
    """Open chat surfaces (CLI, TUI, Desktop, gateway) from the active-session lease file
    ``runtime/active_sessions.json``; only leases whose owning process is still alive. Read
    directly rather than through the registry API, because the registry prunes dead leases on
    read and this command must not write."""
    data, note = _read_json(home / "runtime" / "active_sessions.json")
    if note:
        return [], note
    # Current registries write {"entries": [...]}; older ones a bare list.
    entries = data.get("entries") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return [], "unreadable (unexpected shape)"
    live = [e for e in entries
            if isinstance(e, dict) and pid_alive(e.get("pid"), e.get("process_start_time"))]
    titles = _session_titles(home, [str(e["session_id"]) for e in live if e.get("session_id")])
    out: Items = []
    for entry in live:
        sid = str(entry.get("session_id") or "")
        out.append({
            "kind": "session",
            "surface": entry.get("surface") or entry.get("source") or "?",
            "session_id": sid,
            "title": titles.get(sid),
            "pid": entry.get("pid"),
            "started_at": entry.get("started_at"),
        })
    return out, None


def _live_board_db(root: Path, slug: str) -> Optional[Path]:
    slug = slug.strip().lower()
    if not slug or slug == "default" or not _BOARD_SLUG.match(slug):
        return None
    board = root / "kanban" / "boards" / slug
    meta, _ = _read_json(board / "board.json")
    if not (board / "kanban.db").exists() or (isinstance(meta, dict) and meta.get("archived")):
        return None
    return board / "kanban.db"


def kanban_db_location() -> Path:
    """The current board's ``kanban.db`` by the documented rules (``hermes kanban`` in the CLI
    reference, ``HERMES_KANBAN_*`` in the environment-variable reference): ``HERMES_KANBAN_DB``
    pins the file; otherwise the board is ``HERMES_KANBAN_BOARD``, else ``<root>/kanban/current``,
    else ``default`` (whose DB is ``<root>/kanban.db``), and a named board that does not exist or
    is archived falls back to ``default``. ``<root>`` is ``HERMES_KANBAN_HOME`` or the default
    Hermes root: the board is shared by every profile by design."""
    pin = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if pin:
        return Path(pin).expanduser()
    override = os.environ.get("HERMES_KANBAN_HOME", "").strip()
    if override:
        root = Path(override).expanduser()
    else:
        from hermes_constants import get_default_hermes_root

        root = get_default_hermes_root()
    try:
        current = (root / "kanban" / "current").read_text(encoding="utf-8-sig")
    except OSError:
        current = ""
    for slug in (os.environ.get("HERMES_KANBAN_BOARD", ""), current):
        found = _live_board_db(root, slug)
        if found:
            return found
    return root / "kanban.db"


def collect_kanban(limit: int = 20) -> tuple[Items, Note]:
    """Kanban tasks that are running, blocked or waiting for review on the current board.
    The board is shared by every profile by design, so this section is not profile-scoped."""
    db = kanban_db_location()
    if not db.exists():
        return [], "absent"
    try:
        conn = _connect_ro(db)
        try:
            rows = conn.execute(
                """SELECT id, title, status, assignee, started_at, created_at FROM tasks
                   WHERE status IN ('running', 'blocked', 'review')
                   ORDER BY CASE status WHEN 'blocked' THEN 0 WHEN 'review' THEN 1 ELSE 2 END,
                            COALESCE(started_at, created_at) DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return [], f"unreadable ({exc})"
    return [
        {"kind": "kanban", "id": tid, "title": str(title or "")[:100], "status": status,
         "assignee": assignee, "started_at": started_at, "created_at": created_at}
        for tid, title, status, assignee, started_at, created_at in rows
    ], None
