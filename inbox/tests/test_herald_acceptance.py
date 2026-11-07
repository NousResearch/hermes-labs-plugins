"""Acceptance regressions for configured, scoped Herald reads."""
from pathlib import Path
import json
import pytest
from inbox_plugin import herald, report
from test_herald import _write_ledger, EDGE_ROWS


def test_configured_path_and_supplied_identity_override_ambient_home(hermes_home, tmp_path, monkeypatch):
    _write_ledger(hermes_home)
    db = hermes_home / "hermes-herald.db"
    other = hermes_home / "configured.db"
    db.rename(other)
    (hermes_home / "config.yaml").write_text(
        f"hermes_herald:\n  ledger_file: '{other}'\n  origin_name: felix\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "not-the-supplied-home"))
    result = herald.read_edges(hermes_home)
    assert result["status"] == "read"
    assert result["origin_profile"] == "felix"
    assert {e["edge_id"] for e in result["edges"]} == {"edge-1", "edge-2"}
    data = report.build_inbox(home=hermes_home, fleet=True)
    assert len(data["herald"]) == 2


@pytest.mark.parametrize("setting", ["''", "null", "false"])
def test_falsy_path_uses_native_default(hermes_home, setting):
    _write_ledger(hermes_home)
    (hermes_home / "config.yaml").write_text(
        f"hermes_herald:\n  origin_name: alex\n  ledger_file: {setting}\n")
    assert len(herald.read_edges(hermes_home)["edges"]) == 3


@pytest.mark.parametrize("path", ["${HERMES_HOME}/hermes-herald.db", "$HERMES_HOME/hermes-herald.db"])
def test_profile_home_expansion_never_uses_another_ambient_profile(hermes_home, path, monkeypatch):
    _write_ledger(hermes_home)
    monkeypatch.setenv("HERMES_HOME", "/unrelated-profile")
    (hermes_home / "config.yaml").write_text(
        f"hermes_herald:\n  origin_name: alex\n  ledger_file: '{path}'\n")
    assert len(herald.read_edges(hermes_home)["edges"]) == 3


def test_tilde_path(hermes_home, monkeypatch):
    _write_ledger(hermes_home)
    monkeypatch.setenv("HOME", str(hermes_home))
    monkeypatch.setenv("USERPROFILE", str(hermes_home))  # Path.expanduser uses this on Windows.
    (hermes_home / "config.yaml").write_text(
        "hermes_herald:\n  origin_name: alex\n  ledger_file: '~/hermes-herald.db'\n")
    assert herald.read_edges(hermes_home)["status"] == "read"


def test_unknown_identity_never_reads_all_profiles(hermes_home, monkeypatch):
    _write_ledger(hermes_home)
    (hermes_home / "config.yaml").write_text("{}\n")
    # PM CI can legitimately identify an active HERMES_HOME as native default.
    # Exercise a genuinely unknown resolver result instead of assuming host layout.
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "profile_name_for_home", lambda home: None)
    assert herald.profile_identity(hermes_home) == ""

    def forbidden_read(*args, **kwargs):
        raise AssertionError("unknown identity must not open a source database")

    monkeypatch.setattr(herald.sqlite3, "connect", forbidden_read)
    result = herald.read_edges(hermes_home)
    assert result["status"] != "read"
    assert result["edges"] == []


def test_truncation_reaches_snapshot_and_text(hermes_home, monkeypatch):
    rows = [dict(EDGE_ROWS[0], edge_id=f"edge-{i}", run_id=f"run-{i}") for i in range(25)]
    _write_ledger(hermes_home, rows)
    data = report.build_inbox(home=hermes_home, fleet=True)
    assert data["herald_meta"]["truncated"] is True
    assert "truncated" in report.render_inbox(data).lower()
    assert len(data["herald"]) == 20


def test_safe_exact_linkage_and_no_sensitive_columns(hermes_home):
    _write_ledger(hermes_home)
    result = herald.read_edges(hermes_home)
    edge = next(e for e in result["edges"] if e["edge_id"] == "edge-3")
    assert edge["trace_id"] == "m3" and edge["parent_edge_id"] == "parent-1"
    assert "reasoning" not in edge
    assert "This is a full message" not in json.dumps(result)


def test_invalid_config_does_not_fall_back_to_all_clear(hermes_home):
    _write_ledger(hermes_home)
    (hermes_home / "config.yaml").write_text("hermes_herald: [invalid")
    assert herald.read_edges(hermes_home)["status"] == "unreadable"
