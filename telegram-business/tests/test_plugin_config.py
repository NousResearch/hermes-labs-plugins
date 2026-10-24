"""Plugin config coercion: junk in config.yaml must not break registration.

``register()`` runs before any handler exists, so a raised config error takes the whole
Telegram adapter's Business Mode wiring down with it. Each numeric setting falls back to
its default instead.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from telegram_business_plugin import _as_float, _as_int


class _Ctx:
    def __init__(self) -> None:
        self.wire = None

    def register_telegram_handler(self, fn) -> None:
        self.wire = fn


class _Bot:
    async def send_message(self, **kw):
        return SimpleNamespace(message_id=1)


class _App:
    def __init__(self) -> None:
        self.handlers = []

    def add_handler(self, handler, group=0) -> None:
        self.handlers.append((handler, group))


def _write_config(monkeypatch, tmp_path, block: str) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "plugins:\n  entries:\n    telegram-business:\n" + block,
        encoding="utf-8",
    )


class TestCoercion:
    def test_float_default_on_junk(self):
        assert _as_float("soon", 8.0) == 8.0
        assert _as_float(None, 8.0) == 8.0
        assert _as_float(["x"], 8.0) == 8.0
        assert _as_float("2.5", 8.0) == 2.5

    def test_int_default_on_junk(self):
        assert _as_int("lots", 4000) == 4000
        assert _as_int(None, 4000) == 4000
        assert _as_int("1_200", 4000) == 1200


class TestConfigLoader:
    def test_uses_core_loader_when_pyyaml_is_absent(self, monkeypatch, tmp_path):
        """Core ships hermes_yaml; a bare ``import yaml`` found nothing on CI installs,
        and the defensive except then silently dropped every configured value."""
        pytest.importorskip("hermes_yaml")
        monkeypatch.setitem(sys.modules, "yaml", None)  # simulate no PyYAML
        _write_config(monkeypatch, tmp_path, "      debounce_seconds: '5'\n")

        from telegram_business_plugin import _plugin_config

        assert _plugin_config(None)["debounce_seconds"] == "5"


class TestRegisterSurvivesJunkConfig:
    def test_junk_values_still_register_and_wire(self, monkeypatch, tmp_path):
        # Every value below would raise a bare float()/int().
        _write_config(
            monkeypatch, tmp_path,
            "      debounce_seconds: soon\n      draft_ttl_hours: [24]\n"
            "      max_customer_text_chars: lots\n      owner_persona: 123\n",
        )
        from telegram_business_plugin import register

        ctx = _Ctx()
        register(ctx)  # must not raise
        assert ctx.wire is not None

        app = _App()
        ctx.wire(app, SimpleNamespace(_bot=_Bot()))
        assert app.handlers

    def test_valid_values_are_applied(self, monkeypatch, tmp_path):
        _write_config(monkeypatch, tmp_path, "      debounce_seconds: '3'\n")
        from telegram_business_plugin import register

        ctx = _Ctx()
        register(ctx)

        seen = {}

        async def _noop(**kw):
            return SimpleNamespace(message_id=1)

        adapter = SimpleNamespace(_bot=_Bot())
        app = _App()
        # Capture the manager the wiring builds, then fire one customer message with
        # zero debounce so the draft path runs immediately.
        from telegram_business_plugin import manager as biz

        original_init = biz.BusinessModeManager.__init__

        def _spy_init(self, **kw):
            seen.update(kw)
            original_init(self, **kw)

        monkeypatch.setattr(biz.BusinessModeManager, "__init__", _spy_init)
        ctx.wire(app, adapter)
        assert seen["debounce_seconds"] == 3.0
