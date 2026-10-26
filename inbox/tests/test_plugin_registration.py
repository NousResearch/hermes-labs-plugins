"""Plugin wiring through Hermes' real discovery path against a temp HERMES_HOME: ``hermes inbox``
exists only when the plugin is enabled, and the registered command runs end to end."""

from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

import pytest

from inbox_seed import write_cron

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _install(hermes_home: Path, enabled: bool) -> None:
    dest = hermes_home / "plugins" / "inbox"
    shutil.copytree(PLUGIN_ROOT, dest, ignore=shutil.ignore_patterns("tests", "__pycache__", ".pytest_cache"))
    enabled_yaml = "    - inbox\n" if enabled else "    []\n"
    (hermes_home / "config.yaml").write_text("plugins:\n  enabled:\n" + enabled_yaml, encoding="utf-8")


def _manager():
    from hermes_cli.plugins import PluginManager

    pm = PluginManager()
    pm.discover_and_load()
    return pm


def _parser(pm) -> argparse.ArgumentParser:
    info = pm._cli_commands["inbox"]
    parser = argparse.ArgumentParser(prog="hermes inbox")
    info["setup_fn"](parser)
    parser.set_defaults(func=info["handler_fn"])
    return parser


@pytest.fixture
def enabled_manager(hermes_home):
    _install(hermes_home, enabled=True)
    return _manager()


def test_disabled_plugin_registers_nothing(hermes_home):
    _install(hermes_home, enabled=False)
    manager = _manager()
    assert "inbox" not in manager._cli_commands
    assert "inbox" not in manager._plugin_commands


def test_enabled_plugin_registers_the_command(enabled_manager):
    assert enabled_manager._cli_commands["inbox"]["plugin"] == "inbox"


def test_command_prints_the_inbox(enabled_manager, hermes_home, capsys):
    write_cron(hermes_home, [{"id": "j", "name": "nightly", "enabled": True, "last_status": "error",
                              "last_error": "timeout", "next_run_at": "2026-10-22T03:00:00"}])
    args = _parser(enabled_manager).parse_args([])
    assert args.func(args) == 0
    out = capsys.readouterr().out
    assert "cron 'nightly'  last run FAILED: timeout" in out
    assert "nightly  next: 2026-10-22T03:00:00" in out


def test_json_flag(enabled_manager, hermes_home, capsys):
    args = _parser(enabled_manager).parse_args(["--json"])
    assert args.func(args) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["home"] == str(hermes_home)
    assert data["generated_at"] <= time.time()
    assert set(data) >= {"attention", "in_progress", "finished_undelivered", "cron", "sessions", "sources"}


def test_enabled_plugin_registers_the_slash_command(enabled_manager):
    assert enabled_manager._plugin_commands["inbox"]["plugin"] == "inbox"


def test_slash_command_handler_returns_the_inbox(enabled_manager, hermes_home):
    write_cron(hermes_home, [{"id": "j", "name": "nightly", "enabled": True, "last_status": "error",
                              "last_error": "timeout", "next_run_at": "2026-10-22T03:00:00"}])
    text = enabled_manager._plugin_commands["inbox"]["handler"]("")
    assert isinstance(text, str)
    assert "nightly" in text
    assert len(text) < 4096


def test_slash_command_handler_truncates_oversized_reports(enabled_manager, hermes_home):
    write_cron(hermes_home, [
        {"id": f"job-{i}", "name": f"nightly-{i}-" + "n" * 60, "enabled": True,
         "last_status": "error", "last_error": "timeout-" + "e" * 80,
         "next_run_at": "2026-10-22T03:00:00"}
        for i in range(80)
    ])
    text = enabled_manager._plugin_commands["inbox"]["handler"]("")
    assert len(text) < 4096
    assert text.endswith("\n… (truncated)")
