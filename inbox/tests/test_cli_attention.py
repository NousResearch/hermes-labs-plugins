"""Actual CLI actions acknowledge occurrences, not native task completion."""
import argparse

from inbox_plugin import cli
from inbox_plugin import report
from test_cli_state import call
from test_herald import _write_ledger


def test_acknowledge_is_local_and_reported_without_resolving_source(hermes_home, capsys):
    _write_ledger(hermes_home)
    native = hermes_home / "hermes-herald.db"
    before = native.read_bytes()
    call(capsys, "observe", "--fleet")
    events = call(capsys, "changes")[1]["events"]
    event = next(e for e in events if e["item_id"] == "herald:gabriel:edge-3")
    code, receipt = call(capsys, "ack", str(event["seq"]))
    assert code == 0 and receipt["acknowledged"] is True
    snapshot = report.build_inbox(home=hermes_home, fleet=True)
    local = snapshot["attention_state"][event["item_id"]]
    assert local["visible"] is False and local["acknowledged"] is True
    assert next(e for e in snapshot["herald"] if e["edge_id"] == "edge-3")["status"] == "error"
    assert native.read_bytes() == before


def test_snooze_and_unknown_occurrence_are_bounded(hermes_home, capsys):
    _write_ledger(hermes_home)
    call(capsys, "observe", "--fleet")
    seq = call(capsys, "changes")[1]["events"][0]["seq"]
    assert call(capsys, "snooze", str(seq), "--seconds", "60")[0] == 0
    assert call(capsys, "ack", "999999")[0] == 2
    assert call(capsys, "snooze", str(seq), "--seconds", "nan")[0] == 2


def _view(capsys, *flags):
    parser = argparse.ArgumentParser()
    cli.setup_parser(parser)
    code = cli.handle(parser.parse_args(["--fleet", *flags]))
    return code, capsys.readouterr().out


def test_cli_view_marks_by_default_and_hides_only_with_explicit_flag(hermes_home, capsys):
    _write_ledger(hermes_home)
    call(capsys, "observe", "--fleet")
    events = call(capsys, "changes")[1]["events"]
    edge = next(e for e in events if e["item_id"] == "herald:gabriel:edge-3")
    assert call(capsys, "ack", str(edge["seq"]))[0] == 0

    lanes = call(capsys, "--fleet")[1]["lanes"]
    marked = [i for items in lanes.values() for i in items if i["local_state"] == "acked"]
    assert marked, "an acknowledged item must stay listed, marked, in the default view"
    for flags, shown in (((), True), (("--hide-snoozed",), True), (("--hide-acked",), False)):
        code, text = _view(capsys, *flags)
        assert code == 0
        assert ("local=acked" in text) is shown
