"""``_hermes_home`` follows the host resolver, not a re-implemented env read.

Core resolves the home as context override → ``HERMES_HOME`` → platform default
(``hermes_constants.get_hermes_home``). The plugin only read the env var, so a home
delivered through the context override (profile switch in a core subprocess) was
ignored and the plugin wrote to the default profile.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from telegram_business_plugin import _hermes_home


def test_uses_the_context_override(monkeypatch, tmp_path):
    hermes_constants = pytest.importorskip("hermes_constants")
    override = tmp_path / "profile-b"
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: override)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    assert _hermes_home() == override


def test_env_is_used_when_core_is_not_importable(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "hermes_constants", None)  # imports now raise
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "env-home"))
    assert _hermes_home() == tmp_path / "env-home"


def test_platform_default_when_neither_is_available(monkeypatch):
    monkeypatch.setitem(sys.modules, "hermes_constants", None)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    assert _hermes_home() == Path.home() / ".hermes"
