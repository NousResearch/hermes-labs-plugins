"""Read-only, profile-scoped Herald source. No network calls or source migrations."""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from . import config_yaml as yaml

# Deliberately omit prompts, instructions, reasoning, credentials and result previews.
_COLUMNS = (
    "edge_id", "run_id", "origin_profile", "target_profile", "dispatch_type",
    "delivery", "status", "dispatched_at", "completed_at", "updated_at",
    "trace_id", "parent_edge_id", "origin_session_id", "hop_count", "max_hops",
)
_REQUIRED = {"edge_id", "run_id", "origin_profile", "target_profile", "status", "dispatched_at"}
_HOME_VAR = re.compile(r"\$\{HERMES_HOME\}|\$HERMES_HOME\b")


def _config(home: Path) -> dict:
    path = home / "config.yaml"
    data = yaml.safe_load(path.read_text()) if path.exists() else {}
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError("configuration must be a mapping")
    section = data.get("hermes_herald") or {}
    if not isinstance(section, dict):
        raise ValueError("Herald configuration must be a mapping")
    return section


def profile_identity(home: Path, config: dict | None = None) -> str:
    config = _config(home) if config is None else config
    explicit = config.get("origin_name")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    from hermes_constants import profile_name_for_home
    return profile_name_for_home(home) or ""


def _ledger_path(home: Path, config: dict) -> Path:
    value = config.get("ledger_file")
    if not value:
        return home / "hermes-herald.db"
    if not isinstance(value, str):
        raise ValueError("ledger_file must be a string")
    value = _HOME_VAR.sub(lambda _: str(home), value)
    if "$" in value:
        # Do not consult a credential store or another profile's environment.
        raise ValueError("unsupported unresolved ledger path variable")
    return Path(value).expanduser()


def read_edges(home: Path, limit: int = 20, profile: str | None = None) -> dict:
    home = Path(home).expanduser().resolve()
    cap = max(1, min(int(limit), 500))
    result = {"status": "absent", "edges": [], "limit": cap, "truncated": False,
              "origin_profile": profile or "", "coverage": "local-ledger-only",
              "incoming_coverage": "only edges recorded in this ledger; other origins are not observed"}
    try:
        config = _config(home)
        db = _ledger_path(home, config)
        result["origin_profile"] = identity = profile if profile is not None else profile_identity(home, config)
        if not db.exists():
            return result
        if not identity or not identity.strip():
            raise ValueError("profile identity is unknown; unrestricted reads are refused")
        with_source = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        try:
            with_source.row_factory = sqlite3.Row
            present = {r[1] for r in with_source.execute("PRAGMA table_info(dispatches)")}
            if not _REQUIRED <= present:
                raise ValueError("unsupported Herald dispatch schema")
            columns = [c for c in _COLUMNS if c in present]
            sql = (f"SELECT {','.join(columns)} FROM dispatches "
                   "WHERE origin_profile = ? OR target_profile = ? "
                   "ORDER BY dispatched_at DESC, edge_id DESC LIMIT ?")
            rows = with_source.execute(sql, (identity, identity, cap + 1)).fetchall()
            result.update(status="read", edges=[dict(r) for r in rows[:cap]], truncated=len(rows) > cap)
        finally:
            with_source.close()
    except (sqlite3.Error, OSError, ValueError, yaml.YAMLError) as exc:
        result.update(status="unreadable", edges=[], error=type(exc).__name__)
    return result


def read_fleet(home: Path, limit: int = 20) -> dict:
    """Opt-in view of discoverable local profile ledgers; never contacts peers."""
    from hermes_constants import get_default_hermes_root, named_profile_has_identity, named_profile_is_deleted
    home = Path(home).expanduser().resolve()
    root = get_default_hermes_root().resolve()
    homes = [home]
    sources_truncated = False
    # An arbitrary supplied directory must never cause real-home discovery.
    if home == root or home.parent == root / "profiles":
        candidates = [root]
        profile_dir = root / "profiles"
        if profile_dir.is_dir():
            candidates += sorted((p for p in profile_dir.iterdir()
                                  if p.is_dir() and named_profile_has_identity(p)
                                  and not named_profile_is_deleted(p)), key=lambda p: p.name)
        sources_truncated = len(candidates) > 33
        homes += [p for p in candidates[:33] if p.resolve() != home]
    sources, unique = [], {}
    for source_home in homes:
        data = read_edges(source_home, limit=limit)
        label = str(source_home)
        sources.append({"home": label, **{k: v for k, v in data.items() if k != "edges"}})
        for edge in data["edges"]:
            key = (edge.get("origin_profile"), edge["edge_id"])
            if key not in unique:
                unique[key] = {**edge, "observed_in": [label]}
            else:
                existing = unique[key]
                if any(existing.get(k) != v for k, v in edge.items()):
                    existing["source_conflict"] = True
                if label not in existing["observed_in"]:
                    existing["observed_in"].append(label)
    cap = max(1, min(int(limit), 500))
    edges = sorted(unique.values(), key=lambda e: (str(e.get("dispatched_at", "")), e["edge_id"]), reverse=True)
    readable = any(s["status"] == "read" for s in sources)
    return {"status": "read" if readable else ("unreadable" if any(s["status"] == "unreadable" for s in sources) else "absent"),
            "edges": edges[:cap], "limit": cap,
            "truncated": sources_truncated or len(edges) > cap or any(s["truncated"] for s in sources),
            "sources_truncated": sources_truncated, "sources": sources,
            "origin_profile": sources[0]["origin_profile"], "coverage": "same-host-profile-ledgers",
            "incoming_coverage": "observed local origin ledgers only; remote or inaccessible origins remain unknown"}
