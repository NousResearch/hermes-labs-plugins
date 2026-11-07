"""Ledger text cannot drive the terminal, and a pathological config is unreadable, not a crash."""
import argparse
import json

import pytest
from inbox_plugin import cli, herald, report
from test_herald import EDGE_ROWS, _write_ledger

ESC = "\x1b]0;owned\x07\x1b[2J"


def test_herald_edge_and_thread_fields_are_escaped(hermes_home):
    row = {**EDGE_ROWS[0], "edge_id": "edge" + ESC, "status": "dispatched" + ESC,
           "target_profile": "felix" + ESC, "dispatch_type": "dispatch" + ESC,
           "delivery": "pending" + ESC, "dispatched_at": "2026" + ESC, "trace_id": "m1" + ESC}
    _write_ledger(hermes_home, rows=[row])
    snapshot = report.build_inbox(home=hermes_home, fleet=True)
    assert snapshot["herald"] and snapshot["threads"]
    text = report.render_inbox(snapshot)
    assert "\x1b" not in text and "\x07" not in text
    assert "edge\\u001b]0;owned\\u0007\\u001b[2J" in text
    assert "Work threads" in text and "\\u001b" in text.split("Work threads", 1)[1]


def test_thread_line_escapes_every_ledger_field():
    text = report.render_inbox({"threads": [{"id": "t" + ESC, "execution": "running" + ESC,
                                              "outcome": "unknown" + ESC, "delivery": ["callback" + ESC]}]})
    assert "\x1b" not in text and "\x07" not in text
    assert text.count("\\u001b") == 8


def _too_deep(document):
    raise RecursionError("maximum recursion depth exceeded")


def test_deeply_nested_config_is_unreadable_not_a_crash(hermes_home, monkeypatch):
    (hermes_home / "config.yaml").write_text("hermes_herald: [[[]]]\n")
    monkeypatch.setattr(herald.yaml, "safe_load", _too_deep)
    assert herald.read_edges(hermes_home)["status"] == "unreadable"
    assert herald.read_edges(hermes_home)["error"] == "RecursionError"
    snapshot = report.build_inbox(home=hermes_home)
    assert snapshot["attention_state_status"] == "unreadable"
    assert snapshot["attention_state_error"] == "RecursionError"
    assert "Local attention state unreadable; no items suppressed" in report.render_inbox(snapshot)


@pytest.mark.parametrize("args", [["changes"], ["--json"]])
def test_cli_reports_deep_config_without_traceback(hermes_home, monkeypatch, capsys, args):
    (hermes_home / "config.yaml").write_text("hermes_herald: [[[]]]\n")
    monkeypatch.setattr(herald.yaml, "safe_load", _too_deep)
    parser = argparse.ArgumentParser()
    cli.setup_parser(parser)
    code = cli.handle(parser.parse_args(args))
    out = json.loads(capsys.readouterr().out)
    if args == ["changes"]:
        assert code == 2 and "error" in out
    else:
        assert code == 0 and out["attention_state_status"] == "unreadable"
