"""Conservative attention lanes: classification is never execution permission."""
from __future__ import annotations

LANE_NAMES = ("Needs owner", "Agent can act", "Waiting", "Done", "Unknown")
_WAITING = {"running", "dispatched", "queued", "finalizing", "waiting", "paused", "blocked", "review"}
_SECTIONS = ("native", "attention", "in_progress", "finished_undelivered", "cron", "sessions")


def _text(value):
    return value if isinstance(value, str) and value.strip() else None


def _references(item):
    values = item.get("evidence_refs") or item.get("evidence") or []
    if isinstance(values, str):
        values = [values]
    refs = []
    if isinstance(values, list):
        for value in values:
            if isinstance(value, dict):
                value = value.get("reference") or value.get("path") or value.get("hash")
            if _text(value):
                refs.append({"reference": value, "verified": False})
    return refs


def _local_attention(keys, state):
    states = [state.get(key) for key in keys]
    occurrences = sorted({s["occurrence"] for s in states if isinstance(s, dict)
                          and type(s.get("occurrence")) is int and s["occurrence"] > 0})
    # Unknown/new edges prevent hiding an entire thread behind an old acknowledgment.
    hidden = bool(states) and all(isinstance(s, dict) and s.get("visible") is False for s in states)
    return not hidden, occurrences


def _project(raw, profile, state, *, thread=False):
    kind = "thread" if thread else raw["kind"]
    owner = _text(raw.get("owner")) or _text(raw.get("assignee"))
    if thread:
        targets = sorted({text for value in raw.get("targets", []) if (text := _text(value)) is not None})
        owner = owner or (targets[0] if len(targets) == 1 else None)
        keys = [f"herald:{e.get('origin_profile', '')}:{e.get('edge_id', '')}"
                for e in raw.get("edges", []) if isinstance(e, dict)]
        provenance = [{"source": "herald", "origin_profile": e.get("origin_profile"),
                       "edge_id": e.get("edge_id"), "observed_in": e.get("observed_in", [])}
                      for e in raw.get("edges", []) if isinstance(e, dict)]
    else:
        if owner is None and kind in {"process", "delegation", "cron", "session"}:
            owner = _text(profile)
        keys = [f"{kind}:{raw['id']}"]
        provenance = [{"source": section, "profile": profile} for section in raw["_sources"]]
    execution = (_text(raw.get("execution")) or _text(raw.get("state"))
                 or _text(raw.get("status")) or _text(raw.get("last_status")) or "unknown")
    if raw.get("paused") is True:
        execution = "paused"
    outcome = _text(raw.get("outcome")) or "unknown"
    visible, occurrences = _local_attention(keys, state)
    projected = {"id": raw["id"], "kind": kind, "owner": owner, "execution": execution,
                 "outcome": outcome, "delivery": raw.get("delivery", raw.get("delivery_state", "unknown")),
                 "next_step": _text(raw.get("next_step")), "evidence_refs": _references(raw),
                 "provenance": provenance, "attention_visible": visible, "occurrences": occurrences,
                 "source_conflict": bool(raw.get("source_conflict")),
                 "execution_permission": "not_granted_by_inbox"}
    if projected["source_conflict"]:
        lane = "Unknown"
    elif raw.get("needs_owner") is True or raw.get("approval_required") is True:
        lane = "Needs owner"
    elif raw.get("actionable") is True and owner is not None:
        lane = "Agent can act"
    elif execution in _WAITING:
        lane = "Waiting"
    elif outcome == "accepted":
        lane = "Done"
    else:
        lane = "Unknown"
    return lane, projected


def build_lanes(snapshot: dict) -> dict:
    """Keep exact-ID threads and unlinked native items; preserve all source facts."""
    result = {name: [] for name in LANE_NAMES}
    state = snapshot.get("attention_state") or {}
    profile = snapshot.get("profile")
    records = {}
    for section in _SECTIONS:
        for index, value in enumerate(snapshot.get(section) or []):
            if not isinstance(value, dict):
                continue
            kind = _text(value.get("kind")) or {"cron": "cron", "sessions": "session"}.get(section, "unknown")
            identifier = _text(value.get("id")) or _text(value.get("session_id")) or _text(value.get("job_id"))
            # Missing IDs are not a license to correlate by title, timestamp or fuzzy matching.
            identifier = identifier or f"unidentified:{section}:{index}"
            key = kind, identifier
            if key not in records:
                records[key] = {**value, "kind": kind, "id": identifier, "_sources": [section]}
            else:
                old = records[key]
                for field in ("state", "status", "execution", "outcome", "owner", "assignee", "delivery_state"):
                    if old.get(field) is not None and value.get(field) is not None and old[field] != value[field]:
                        old["source_conflict"] = True
                old["_sources"].append(section)
    for value in records.values():
        name, projected = _project(value, profile, state)
        result[name].append(projected)
    seen_threads = set()
    for value in snapshot.get("threads") or []:
        if not isinstance(value, dict) or not _text(value.get("id")) or value["id"] in seen_threads:
            continue
        seen_threads.add(value["id"])
        name, projected = _project(value, profile, state, thread=True)
        result[name].append(projected)
    for items in result.values():
        items.sort(key=lambda item: (item["kind"], item["id"]))
    return result
