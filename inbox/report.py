"""Assemble the inbox snapshot and render it as text."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

from . import collectors as c
from . import herald
from .threads import build_threads
from .lanes import build_lanes, local_mark

# Source name -> what the user sees in the "not read" footer.
SOURCE_LABELS = {
    "processes": "background processes (processes.json)",
    "delegations": "async delegations (state.db)",
    "cron": "cron jobs (cron/jobs.json)",
    "sessions": "open sessions (runtime/active_sessions.json)",
    "kanban": "kanban board (kanban.db)",
    "herald": "Herald edges (hermes-herald.db)",
}

_STALLED_DELEGATION_STATES = ("stalling", "stalled", "unknown")
_RUNNING_DELEGATION_STATES = ("running", "finalizing")


def build_inbox(home: Optional[Path] = None, fleet: bool = False, *,
                hide_acked: bool = False, hide_snoozed: bool = False) -> dict[str, Any]:
    """The full snapshot as JSON-serialisable data. ``home`` defaults to the active profile's.

    Acknowledged and snoozed items stay in every view, marked with ``local_state``. Only the
    explicit ``hide_acked`` / ``hide_snoozed`` filters leave them out of ``visible_attention``
    and the rendered lanes; the raw ``attention`` list is never filtered.
    """
    if home is None:
        from hermes_constants import get_hermes_home

        home = get_hermes_home()
    processes, n_proc = c.collect_processes(home)
    delegations, n_del = c.collect_delegations(home, limit=21, include_completed=True)
    native_truncated = len(delegations) > 20
    delegations = delegations[:20]
    if native_truncated:
        n_del = "truncated (20 returned)"
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

    sources = {
        name: note or "read"
        for name, note in (("processes", n_proc), ("delegations", n_del), ("cron", n_cron),
                           ("sessions", n_sess), ("kanban", n_kan))
    }

    herald_edges: list[dict[str, Any]] = []
    herald_meta: dict[str, Any] = {}
    if fleet:
        herald_data = herald.read_fleet(home)
        sources["herald"] = herald_data["status"]
        herald_edges = herald_data["edges"]
        herald_meta = {k: v for k, v in herald_data.items() if k != "edges"}

    snapshot = {
        "generated_at": time.time(),
        "home": str(home),
        "attention": attention,
        "in_progress": in_progress,
        "finished_undelivered": finished,
        "cron": cron_jobs,
        "sessions": sessions,
        "native": processes + delegations + kanban,
        "native_limits": {"delegations": {"limit": 20, "truncated": native_truncated,
                                            "completed_window_seconds": c.DROPPED_WINDOW_S}},
        "sources": sources,
        "herald": herald_edges,
        "herald_meta": herald_meta,
        "threads": build_threads(herald_edges),
    }
    _attach_attention(snapshot, home)
    snapshot["lanes"] = build_lanes(snapshot)
    local = snapshot["attention_state"]
    snapshot["attention_filter"] = {"hide_acked": bool(hide_acked), "hide_snoozed": bool(hide_snoozed)}
    hidden = _hidden_states(snapshot)
    marked = [{**item, "local_state": local_mark([local.get(f"{item.get('kind')}:{item.get('id')}")])}
              for item in attention]
    snapshot["visible_attention"] = [item for item in marked if item["local_state"] not in hidden]
    return snapshot


def _hidden_states(data: dict[str, Any]) -> set[str]:
    applied = data.get("attention_filter") or {}
    return ({"acked"} if applied.get("hide_acked") else set()) | (
        {"snoozed"} if applied.get("hide_snoozed") else set())


def _filter_flags(data: dict[str, Any]) -> str:
    applied = data.get("attention_filter") or {}
    return " ".join(flag for flag, key in (("--hide-acked", "hide_acked"), ("--hide-snoozed", "hide_snoozed"))
                    if applied.get(key))


def _attach_attention(snapshot: dict, home: Path) -> None:
    from .state import InboxState, InboxStateError
    from .observations import records
    from hermes_yaml import YAMLError
    snapshot["attention_state"] = {}
    snapshot["profile"] = None
    snapshot["attention_state_status"] = "unknown"
    try:
        profile = herald.profile_identity(home)
        snapshot["profile"] = profile
        if not profile:
            return
        from .attention import AttentionState
        state = InboxState(home, profile)
        snapshot["attention_state"] = AttentionState(state).states_for(records(snapshot))
        snapshot["attention_state_status"] = "read" if state.db_path.exists() else "absent"
    except (InboxStateError, OSError, ValueError, YAMLError, ImportError, RecursionError) as exc:
        snapshot["attention_state_status"] = "unreadable"
        snapshot["attention_state_error"] = type(exc).__name__


def _inert(value: Any) -> str:
    """JSON-escape a source-controlled value so terminal control sequences stay inert."""
    return json.dumps(str(value), ensure_ascii=True)[1:-1]


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
    line = fn(item, now) if fn else json.dumps(item, default=str)
    # Same key=value form as the work-thread lines; marked items are shown, not dropped.
    if item.get("local_state") in ("acked", "snoozed"):
        line += f"  local={item['local_state']}"
    return line


def _section(lines: list[str], title: str, items: list, mark: str, now: float, empty: str = "") -> None:
    if not items and not empty:
        return
    lines.append(f"  ◆ {title} ({len(items)})")
    lines += [f"    {mark} {describe(i, now)}" for i in items] or [f"    {empty}"]
    lines.append("")


def render_inbox(data: dict[str, Any]) -> str:
    now = data.get("generated_at") or time.time()
    lines: list[str] = [""]
    meta = data.get("herald_meta") or {}
    incomplete = (any(v != "read" for v in (data.get("sources") or {}).values())
                  or meta.get("truncated")
                  or any(s.get("status") != "read" for s in meta.get("sources", []))
                  or data.get("attention_state_status") in ("unknown", "unreadable"))
    empty_attention = "no recorded attention items — coverage incomplete" if incomplete else "no recorded native attention items"
    empty_running = "no recorded running items — coverage incomplete" if incomplete else "nothing running in the background"
    visible_attention = data.get("visible_attention", data.get("attention")) or []
    _section(lines, "Needs attention", visible_attention, "⚠", now, empty_attention)
    hidden_native = len(data.get("attention") or []) - len(visible_attention)
    if hidden_native:
        lines.append(f"  {hidden_native} native attention item(s) hidden by {_filter_flags(data) or 'a filter'}; "
                     "source state unchanged.\n")
    marked_native = sum(1 for i in visible_attention if i.get("local_state") in ("acked", "snoozed"))
    if marked_native:
        lines.append(f"  {marked_native} shown item(s) marked acked/snoozed locally; source state unchanged.\n")
    _section(lines, "In progress", data.get("in_progress") or [], "◐", now, empty_running)
    _section(lines, "Finished, result not yet delivered", data.get("finished_undelivered") or [], "✔", now)

    active = [j for j in data.get("cron") or [] if j.get("enabled") and not j.get("paused")]
    if active:
        upcoming = sorted((j for j in active if j.get("next_run_at")),
                          key=lambda j: str(j["next_run_at"]))[:5]
        lines.append(f"  ◆ Scheduled ({len(active)} active job(s))")
        lines += [f"    ⏱ {j['name']}  next: {j['next_run_at']}" for j in upcoming]
        lines.append("")

    _section(lines, "Open surfaces", data.get("sessions") or [], "◎", now)

    herald_meta = data.get("herald_meta") or {}
    if herald_meta:
        lines.append(f"  Herald coverage: {herald_meta.get('coverage', 'unknown')}; "
                     f"{herald_meta.get('incoming_coverage', 'incoming coverage unknown')}")
        if herald_meta.get("truncated"):
            lines.append(f"  Herald results truncated at {herald_meta.get('limit')} edges.")

    herald_edges = data.get("herald") or []
    if herald_edges:
        lines.append(f"  ◆ Herald edges ({len(herald_edges)})")
        # Ledger fields are source-controlled: escape them like the lane lines below.
        lines += [
            f"    ➤ {_inert(e['edge_id'])}  [{_inert(e.get('dispatch_type'))}/{_inert(e.get('delivery'))}]"
            f" {_inert(e.get('status'))}  hop={_inert(e.get('hop_count'))}"
            f" origin={_inert(e.get('origin_profile'))} target={_inert(e.get('target_profile'))}"
            f"  dispatched={_inert(e.get('dispatched_at'))}"
            for e in herald_edges
        ]
        lines.append("")

    work_threads = data.get("threads") or []
    if work_threads:
        lines.append(f"  ◆ Work threads ({len(work_threads)})")
        for thread in work_threads:
            delivery = ",".join(_inert(d) for d in thread["delivery"])
            lines.append(f"    · {_inert(thread['id'])} execution={_inert(thread['execution'])} "
                         f"outcome={_inert(thread['outcome'])} delivery={delivery}")
        lines.append("")

    hidden_states = _hidden_states(data)
    for name, items in (data.get("lanes") or {}).items():
        visible = [item for item in items if item.get("local_state") not in hidden_states]
        lines.append(f"  ◆ {name} ({len(visible)} visible / {len(items)} recorded)")
        for item in visible:
            # JSON escaping keeps source-controlled terminal sequences inert.
            label = _inert(item["id"])
            owner = _inert(item.get("owner") or "unknown")
            state = _inert(item["execution"])
            occurrence = ",".join(str(v) for v in item.get("occurrences", [])) or "unobserved"
            mark = f" local={item['local_state']}" if item.get("local_state") in ("acked", "snoozed") else ""
            lines.append(f"    · {item['kind']} {label} owner={owner} execution={state} occurrence={occurrence}{mark}")
        lines.append("")
    if data.get("lanes"):
        lines.append("  Recorded snapshots, not live heartbeats. Classification grants no execution permission.")
        lines.append("  Completion is not accepted outcome; evidence references remain unverified.\n")
    if data.get("attention_state_status") == "unreadable":
        lines.append("  Local attention state unreadable; no items suppressed.\n")
    skipped = [f"{SOURCE_LABELS.get(name, name)}: {note}"
               for name, note in (data.get("sources") or {}).items() if note != "read"]
    if skipped:
        lines.append("  Not read:")
        lines += [f"    · {s}" for s in skipped]
        lines.append("")
    return "\n".join(lines)
