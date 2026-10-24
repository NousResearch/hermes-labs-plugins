"""Fleet coverage regression: discover native profile homes, never infer remote coverage."""
from inbox_plugin import herald, report
from test_herald import _write_ledger, EDGE_ROWS


def test_fleet_observes_incoming_from_another_origins_ledger(hermes_home, monkeypatch):
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda **kw: hermes_home)
    incoming_home = hermes_home / "profiles" / "gabriel"
    incoming_home.mkdir(parents=True)
    _write_ledger(hermes_home, [EDGE_ROWS[0]])
    incoming = dict(EDGE_ROWS[2], edge_id="separate-incoming", origin_profile="gabriel", target_profile="alex")
    _write_ledger(incoming_home, [incoming])
    (incoming_home / "config.yaml").write_text("hermes_herald:\n  origin_name: gabriel\n")
    data = report.build_inbox(home=hermes_home, fleet=True)
    assert {e["edge_id"] for e in data["herald"]} == {"edge-1", "separate-incoming"}
    assert data["herald_meta"]["coverage"] == "same-host-profile-ledgers"
    assert len(data["herald_meta"]["sources"]) == 2


def test_shared_ledger_edges_are_deduplicated_and_sources_disclosed(hermes_home, monkeypatch):
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda **kw: hermes_home)
    _write_ledger(hermes_home)
    other = hermes_home / "profiles" / "felix"
    other.mkdir(parents=True)
    (other / "config.yaml").write_text(
        f"hermes_herald:\n  origin_name: felix\n  ledger_file: '{hermes_home / 'hermes-herald.db'}'\n")
    data = herald.read_fleet(hermes_home)
    assert len(data["edges"]) == 3
    assert len(data["sources"]) == 2
    assert "remote" in data["incoming_coverage"]
