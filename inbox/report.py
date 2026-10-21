"""Assemble the inbox snapshot and render it as text."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

from . import collectors as c

# Source name -> what the user sees in the "not read" footer.
SOURCE_LABELS = {
    "processes": "background processes (processes.json)",
    "delegations": "async delegations (state.db)",
    "cron": "cron jobs (cron/jobs.json)",
    "sessions": "open sessions (runtime/active_sessions.json)",
    "kanban": "kanban board (kanban.db)",
}

_STALLED_DELEGATION_STATES = ("stalling", "stalled", "unknown")
_RUNNING_DELEGATION_STATES = ("running", "finalizing")


def build_inbox(home: Optional[Path] = None) -> dict[str, Any]:
    """The full snapshot as JSON-serialisable data. ``home`` defaults to the active profile's."""
    if home is None:
        from hermes_constants import get_hermes_home

        home = get_hermes_home()
    processes, n_proc = c.collect_processes(home)
    delegations, n_del = c.collect_delegations(home)
    cron_jobs, n_cron = c.collect_cron(home)
    sessions, n_sess = c.collect_sessions(home)
    kanban, n_kan = c.collect_kanban()

    attention: list[dict[str, Any]] = []
    attention += [d for d in delegations
                  if d["state"] in _STALLED_DELEGATION_STATES or d["delivery_state"] == "dropped"]
    attention += [j for j in cron_jobs if j["needs_attention"] or (j["paused"] and j["paused_reason"])]
    # Checkpointed as running but the PID is gone: an orphaned entry from an owner that crashed.
    attention += [p for p in processes if p["alive"] is False]
    attention += [t for t in kanban if t["status"] in ("blocked", "review")]

    in_progress: list[dict[str, Any]] = []
    in_progress += [p for p in processes if p["alive"] is not False]
    in_progress += [d for d in delegations if d["state"] in _RUNNING_DELEGATION_STATES]
    in_progress += [t for t in kanban if t["status"] == "running"]

    finished = [d for d in delegations
                if d["state"] not in c.LIVE_DELEGATION_STATES and d["delivery_state"] == "pending"]

    return {
        "generated_at": time.time(),
        "home": str(home),
        "attention": attention,
        "in_progress": in_progress,
        "finished_undelivered": finished,
        "cron": cron_jobs,
        "sessions": sessions,
        "sources": {
            name: note or "read"
            for name, note in (("processes", n_proc), ("delegations", n_del), ("cron", n_cron),
                               ("sessions", n_sess), ("kanban", n_kan))
        },
    }


def fmt_age(seconds: Any) -> str:
    if not isinstance(seconds, (int, float)):
        return "?"
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h{m:02d}m"
    d, h = divmod(h, 24)
    return f"{d}d{h}h"


def _describe_process(item: dict[str, Any], now: float) -> str:
    alive = item.get("alive")
    state = "running" if alive else "GONE (stale checkpoint)" if alive is False else "sandboxed"
    return f"process {item['id']}  [{state}, {fmt_age(item.get('age_seconds'))}]  {item['command']}"


def _describe_delegation(item: dict[str, Any], now: float) -> str:
    when = item.get("completed_at") or item.get("dispatched_at")
    age = fmt_age(now - when) if isinstance(when, (int, float)) else "?"
    line = f"delegation {item['id']}  [{item['state']}, {age} ago]"
    if item.get("delivery_state") == "dropped":
        line += "  (result DROPPED after failed deliveries)"
    elif item.get("delivery_state") == "pending" and item["state"] not in _RUNNING_DELEGATION_STATES:
        line += "  (result awaiting delivery)"
    if item.get("goal"):
        line += f"  {item['goal']}"
    return line


def _describe_cron(item: dict[str, Any], now: float) -> str:
    line = f"cron '{item['name']}'"
    if item.get("last_error"):
        line += f"  last run FAILED: {item['last_error']}"
    elif item.get("last_status") not in c.OK_CRON_STATUSES:
        line += f"  last status: {item['last_status']}"
    elif item.get("paused"):
        line += f"  paused ({item.get('paused_reason') or 'manually'})"
    return line


def _describe_session(item: dict[str, Any], now: float) -> str:
    label = item.get("title") or item.get("session_id") or ""
    return f"session [{item.get('surface')}] pid={item.get('pid')}  {label}".rstrip()


def _describe_kanban(item: dict[str, Any], now: float) -> str:
    who = f" @{item['assignee']}" if item.get("assignee") else ""
    return f"kanban {item['id']}  [{item['status']}{who}]  {item['title']}"


_DESCRIBERS = {
    "process": _describe_process,
    "delegation": _describe_delegation,
    "cron": _describe_cron,
    "session": _describe_session,
    "kanban": _describe_kanban,
}


def describe(item: dict[str, Any], now: float) -> str:
    fn = _DESCRIBERS.get(str(item.get("kind")))
    return fn(item, now) if fn else json.dumps(item, default=str)


def _section(lines: list[str], title: str, items: list, mark: str, now: float, empty: str = "") -> None:
    if not items and not empty:
        return
    lines.append(f"  ◆ {title} ({len(items)})")
    lines += [f"    {mark} {describe(i, now)}" for i in items] or [f"    {empty}"]
    lines.append("")


def render_inbox(data: dict[str, Any]) -> str:
    now = data.get("generated_at") or time.time()
    lines: list[str] = [""]
    _section(lines, "Needs attention", data.get("attention") or [], "⚠", now, "nothing — all clear")
    _section(lines, "In progress", data.get("in_progress") or [], "◐", now,
             "nothing running in the background")
    _section(lines, "Finished, result not yet delivered", data.get("finished_undelivered") or [], "✔", now)

    active = [j for j in data.get("cron") or [] if j.get("enabled") and not j.get("paused")]
    if active:
        upcoming = sorted((j for j in active if j.get("next_run_at")),
                          key=lambda j: str(j["next_run_at"]))[:5]
        lines.append(f"  ◆ Scheduled ({len(active)} active job(s))")
        lines += [f"    ⏱ {j['name']}  next: {j['next_run_at']}" for j in upcoming]
        lines.append("")

    _section(lines, "Open surfaces", data.get("sessions") or [], "◎", now)

    skipped = [f"{SOURCE_LABELS.get(name, name)}: {note}"
               for name, note in (data.get("sources") or {}).items() if note != "read"]
    if skipped:
        lines.append("  Not read:")
        lines += [f"    · {s}" for s in skipped]
        lines.append("")
    return "\n".join(lines)
