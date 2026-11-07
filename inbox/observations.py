"""Minimal durable observation projection; never persist prompts or raw outputs."""
from __future__ import annotations
import hashlib
import json

_FIELDS = ("state", "status", "last_status", "last_run_at", "last_error", "paused",
           "paused_reason", "delivery_state", "completed_at", "started_at", "alive",
           "assignee", "trace_id", "parent_edge_id", "source_conflict")


def records(snapshot: dict) -> list[dict]:
    items = {}
    def add(key, kind, state, facts):
        raw = json.dumps(facts, sort_keys=True, separators=(",", ":"), default=str)
        items[key] = {"id": key, "kind": kind, "state": str(state or "unknown"),
                      "fingerprint": hashlib.sha256(raw.encode()).hexdigest()}
    for section in ("native", "attention", "in_progress", "finished_undelivered", "cron", "sessions"):
        for item in snapshot.get(section, []):
            kind = item.get("kind", "unknown")
            identity = item.get("id") or item.get("session_id")
            if identity is None:
                continue
            state = item.get("state") or item.get("status") or item.get("last_status") or "unknown"
            add(f"{kind}:{identity}", kind, state, {k: item.get(k) for k in _FIELDS})
    for edge in snapshot.get("herald", []):
        add(f"herald:{edge.get('origin_profile', '')}:{edge['edge_id']}", "herald", edge.get("status"),
            {k: edge.get(k) for k in (*_FIELDS, "delivery")})
    for source, status in snapshot.get("sources", {}).items():
        facts = {"status": status}
        if source == "herald":
            facts["truncated"] = snapshot.get("herald_meta", {}).get("truncated", False)
            facts["sources"] = [{"profile": s.get("origin_profile"), "status": s.get("status")}
                               for s in snapshot.get("herald_meta", {}).get("sources", [])]
        add("source:" + source, "source", status, facts)
    return [items[k] for k in sorted(items)]
