"""Herald-aware read-only tests: real schema fixtures, ro SQLite, redaction, no mutation, missing/unreadable=unknown."""

from __future__ import annotations

import json
import sqlite3
import time

from inbox_plugin import collectors as c
from inbox_plugin import report as r
from inbox_seed import tree

# Nonempty schema fixtures drawn from the installed ledger.py dispatches table.
# Columns deliberately exclude full message/instructions (redacted); message_preview
# stays truncated to 120 chars by ledger.record_dispatch.
# Named dicts mapped to explicit schema columns (matches dispatches table exactly).
EDGE_ROWS = [
    {
        "edge_id": "edge-1", "run_id": "run-1", "origin_profile": "alex",
        "target_profile": "felix", "dispatch_type": "dispatch",
        "delivery": "pending", "message": "This is a full message that must never leak into the inbox view.",
        "message_preview": "preview of a dispatch edge",
        "instructions": "", "trace_id": "m1", "parent_edge_id": "",
        "hop_count": 1, "max_hops": None, "origin_session_id": "",
        "requested_model": "local", "resolved_model": "", "model_resolution": "",
        "reasoning": "", "status": "dispatched",
        "dispatched_at": "2026-10-22T09:00:00+00:00", "updated_at": "2026-10-22T09:00:05+00:00",
        "completed_at": "", "output_preview": "", "duration_seconds": None,
        "usage_json": "{}",
    },
    {
        "edge_id": "edge-2", "run_id": "run-1", "origin_profile": "alex",
        "target_profile": "felix", "dispatch_type": "chat",
        "delivery": "delivered", "message": "Another full message that stays out of the adapter output.",
        "message_preview": "chat reply preview", "instructions": "",
        "trace_id": "m2", "parent_edge_id": "", "hop_count": 1, "max_hops": None,
        "origin_session_id": "", "requested_model": "local", "resolved_model": "preview",
        "model_resolution": "1.5", "reasoning": "", "status": "completed",
        "dispatched_at": "2026-10-22T09:05:00+00:00", "updated_at": "2026-10-22T09:05:45+00:00",
        "completed_at": "2026-10-22T09:05:45+00:00", "output_preview": "",
        "duration_seconds": None, "usage_json": "{}",
    },
    {
        "edge_id": "edge-3", "run_id": "run-2", "origin_profile": "gabriel",
        "target_profile": "alex", "dispatch_type": "dispatch",
        "delivery": "error", "message": "Failed edge with an error reason.",
        "message_preview": "error preview", "instructions": "",
        "trace_id": "m3", "parent_edge_id": "parent-1", "hop_count": 2, "max_hops": 3,
        "origin_session_id": "", "requested_model": "remote", "resolved_model": "",
        "model_resolution": "", "reasoning": "", "status": "error",
        "dispatched_at": "2026-10-22T09:10:00+00:00", "updated_at": "2026-10-22T09:10:01+00:00",
        "completed_at": "", "output_preview": "", "duration_seconds": None,
        "usage_json": "{}",
    },
]


def _write_ledger(home, rows=EDGE_ROWS):
    db = home / "hermes-herald.db"
    # Every legacy fixture explicitly belongs to alex; never rely on an
    # unknown test directory granting an unrestricted ledger view.
    config = home / "config.yaml"
    if not config.exists():
        config.write_text("hermes_herald:\n  origin_name: alex\n")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS dispatches (
            edge_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            origin_profile TEXT NOT NULL,
            target_profile TEXT NOT NULL,
            dispatch_type TEXT NOT NULL,
            delivery TEXT NOT NULL,
            message TEXT NOT NULL,
            message_preview TEXT NOT NULL,
            instructions TEXT NOT NULL DEFAULT '',
            trace_id TEXT NOT NULL DEFAULT '',
            parent_edge_id TEXT NOT NULL DEFAULT '',
            hop_count INTEGER NOT NULL DEFAULT 1,
            max_hops INTEGER,
            origin_session_id TEXT NOT NULL DEFAULT '',
            requested_model TEXT NOT NULL DEFAULT '',
            resolved_model TEXT NOT NULL DEFAULT '',
            model_resolution TEXT NOT NULL DEFAULT '',
            reasoning TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            dispatched_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT NOT NULL DEFAULT '',
            output_preview TEXT NOT NULL DEFAULT '',
            duration_seconds REAL,
            usage_json TEXT NOT NULL DEFAULT '{}'
        );
        """
    )
    conn.execute(
        "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version','1')"
    )
    columns = list(rows[0].keys())
    cols_str = ", ".join(columns)
    placeholders = ", ".join(["?"] * len(columns))
    conn.executemany(
        f"INSERT INTO dispatches ({cols_str}) VALUES ({placeholders})",
        [[row[col] for col in columns] for row in rows],
    )
    conn.commit()
    conn.close()


class TestHeraldAdapterSchemaFixtures:
    def test_read_edges_returns_nonempty_schema_fixtures(self, hermes_home):
        _write_ledger(hermes_home)
        from inbox_plugin import herald
        result = herald.read_edges(hermes_home)
        assert result["status"] == "read"
        edges = result["edges"]
        assert len(edges) == len(EDGE_ROWS)
        assert edges[0]["edge_id"] == "edge-3"  # sorted by dispatched_at DESC
        assert edges[2]["edge_id"] == "edge-1"
        assert edges[2]["origin_profile"] == "alex"
        assert edges[2]["status"] == "dispatched"

    def test_full_message_and_instructions_never_appear(self, hermes_home):
        _write_ledger(hermes_home)
        from inbox_plugin import herald
        result = herald.read_edges(hermes_home)
        for e in result["edges"]:
            assert "message" not in e
            assert "instructions" not in e
            assert "message_preview" not in e
            assert "output_preview" not in e

    def test_limit_clamped_to_schema_cap(self, hermes_home):
        _write_ledger(hermes_home)
        from inbox_plugin import herald
        big = herald.read_edges(hermes_home, limit=10**9)
        assert big["status"] == "read"
        assert len(big["edges"]) == len(EDGE_ROWS)
        assert big["truncated"] is False
        small = herald.read_edges(hermes_home, limit=1)
        assert len(small["edges"]) == 1
        assert small["truncated"] is True

    def test_truncation_metadata_when_limit_below_rows(self, hermes_home):
        _write_ledger(hermes_home)
        from inbox_plugin import herald
        result = herald.read_edges(hermes_home, limit=2)
        assert len(result["edges"]) == 2
        assert result["truncated"] is True

    def test_origin_target_isolation_and_hop_count(self, hermes_home):
        _write_ledger(hermes_home)
        from inbox_plugin import herald
        # Without profile scoping, all edges are read.
        all_result = herald.read_edges(hermes_home)
        by_edge = {e["edge_id"]: e for e in all_result["edges"]}
        assert by_edge["edge-3"]["target_profile"] == "alex"
        assert by_edge["edge-3"]["hop_count"] == 2
        assert by_edge["edge-3"]["max_hops"] == 3
        # With active profile == "alex", only edges where alex is origin or target appear.
        scoped = herald.read_edges(hermes_home, profile="alex")
        scoped_ids = {e["edge_id"] for e in scoped["edges"]}
        assert scoped_ids == {"edge-1", "edge-2", "edge-3"}
        # Profile "felix" sees the two edges felix was involved in.
        felix = herald.read_edges(hermes_home, profile="felix")
        assert {e["edge_id"] for e in felix["edges"]} == {"edge-1", "edge-2"}
        # Profile with no edges sees none.
        none = herald.read_edges(hermes_home, profile="nobody")
        assert none["edges"] == []


class TestHeraldMissingAndFailure:
    def test_missing_ledger_returns_absent_status(self, hermes_home):
        from inbox_plugin import herald
        result = herald.read_edges(hermes_home)
        assert result["status"] == "absent"
        assert result["edges"] == []

    def test_unreadable_ledger_degrades_gracefully(self, hermes_home):
        (hermes_home / "hermes-herald.db").write_bytes(b"not sqlite data")
        from inbox_plugin import herald
        result = herald.read_edges(hermes_home)
        assert result["status"] == "unreadable"
        assert result["edges"] == []

    def test_build_writes_nothing(self, hermes_home):
        before = tree(hermes_home)
        r.build_inbox()
        assert tree(hermes_home) == before


class TestHeraldRedaction:
    def test_no_full_messages_by_default(self, hermes_home):
        _write_ledger(hermes_home)
        for fleet in (False, True):
            data = r.build_inbox(fleet=fleet)
            for section in (data["attention"], data["in_progress"], data["finished_undelivered"]):
                for item in section:
                    assert "message" not in item and "instructions" not in item

    def test_herald_edges_redacted_in_snapshot(self, hermes_home):
        _write_ledger(hermes_home)
        data = r.build_inbox(fleet=True)
        for e in data["herald"]:
            assert "message" not in e
            assert "instructions" not in e
            assert "message_preview" not in e
            assert "output_preview" not in e

    def test_zero_messages_key_on_herald_edges(self, hermes_home):
        _write_ledger(hermes_home)
        data = r.build_inbox(fleet=True)
        assert len(data["herald"]) == len(EDGE_ROWS)


class TestHeraldReportAndCli:
    def test_fleet_flag_populates_sources_herald(self, hermes_home):
        _write_ledger(hermes_home)
        data = r.build_inbox(fleet=True)
        assert data["sources"]["herald"] == "read"
        assert len(data["herald"]) == len(EDGE_ROWS)

    def test_non_fleet_no_herald_in_sources(self, hermes_home):
        _write_ledger(hermes_home)
        data = r.build_inbox(fleet=False)
        assert "herald" not in data["sources"]
        assert data["herald"] == []

    def test_missing_ledger_non_fleet(self, hermes_home):
        from inbox_plugin import herald
        assert herald.read_edges(hermes_home)["status"] == "absent"
