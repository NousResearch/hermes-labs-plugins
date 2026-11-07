"""Public CLI acceptance for explicit, profile-local observations."""
import argparse
import json
from inbox_plugin import cli
from test_herald import _write_ledger


def call(capsys, *args):
    parser = argparse.ArgumentParser()
    cli.setup_parser(parser)
    code = cli.handle(parser.parse_args([*args, "--json"]))
    return code, json.loads(capsys.readouterr().out)


def test_observe_is_idempotent_and_view_does_not_mark_seen(hermes_home, capsys):
    _write_ledger(hermes_home)
    before = (hermes_home / "hermes-herald.db").read_bytes()
    code, first = call(capsys, "observe", "--fleet")
    assert code == 0 and first["created"] > 0
    assert (hermes_home / "hermes-herald.db").read_bytes() == before
    assert call(capsys, "observe", "--fleet")[1]["created"] == 0
    code, changes = call(capsys, "changes", "--consumer", "owner")
    assert code == 0 and changes["events"] and changes["cursor"] == 0
    call(capsys, "--fleet")
    assert call(capsys, "changes")[1]["cursor"] == 0
    call(capsys, "mark-seen", "--consumer", "owner")
    assert call(capsys, "changes", "--consumer", "owner")[1]["events"] == []
    assert call(capsys, "changes", "--consumer", "other")[1]["events"]


def test_read_changes_does_not_create_a_database(hermes_home, capsys):
    _write_ledger(hermes_home)
    assert call(capsys, "changes")[1]["events"] == []
    assert not (hermes_home / "inbox").exists()


def test_invalid_consumer_is_error_not_a_path(hermes_home, capsys):
    _write_ledger(hermes_home)
    code, data = call(capsys, "changes", "--consumer", "../other")
    assert code == 2 and "error" in data
    assert not (hermes_home / "inbox").exists()
