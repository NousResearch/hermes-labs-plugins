"""Yuanbao ships as a catalog platform plugin: registry entry shape + no core wiring left."""

import importlib

import pytest


class _Ctx:
    def __init__(self):
        self.kwargs = None
        self.tools = []
        self.skills = []

    def register_platform(self, **kwargs):
        self.kwargs = kwargs

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)

    def register_skill(self, name, path, description=""):
        self.skills.append((name, path, description))


def test_register_shape_and_core_wiring_removed(monkeypatch):
    register_mod = importlib.import_module("yuanbao_plugin.register")
    from yuanbao_plugin import register
    from yuanbao_plugin.adapter import YuanbaoAdapter
    from yuanbao_plugin.register import _standalone_send

    # The guard keeps the plugin inert on a core that still bundles Yuanbao; this pins the active shape.
    monkeypatch.setattr(register_mod, "core_ships_yuanbao", lambda: False)
    ctx = _Ctx()
    register(ctx)
    kw = ctx.kwargs
    assert kw["name"] == "yuanbao" and kw["label"] == "Yuanbao" and kw["emoji"] == "💎"
    assert kw["adapter_factory"] is YuanbaoAdapter
    assert kw["standalone_sender_fn"] is _standalone_send
    assert kw["required_env"] == ["YUANBAO_APP_ID", "YUANBAO_APP_SECRET"]
    assert kw["cron_deliver_env_var"] == "YUANBAO_HOME_CHANNEL"
    assert kw["install_hint"] == "Yuanbao: websockets not installed. Run: pip install websockets"
    # /update was never allowed from Yuanbao (absent from _UPDATE_ALLOWED_PLATFORMS).
    assert kw["allow_update_command"] is False
    assert callable(kw["setup_fn"]) and callable(kw["is_connected"])

    # Media sends bypass the live adapter for the standalone sender (was a _PLUGIN_STANDALONE_MEDIA row).
    assert kw["standalone_send"].media is True


@pytest.mark.parametrize("module, name", [
    ("gateway.run", "_BUILTIN_ADAPTERS"),
    ("hermes_cli.gateway_setup_wizard", "_PLATFORMS"),
    ("tools.send_message_tool", "_CHUNKED_ROUTES"),
])
def test_no_core_wiring_left(module, name):
    """Core's private per-platform tables no longer name Yuanbao (skipped once core renames one)."""
    table = getattr(importlib.import_module(module), name, None)
    if table is None:
        pytest.skip(f"{module}.{name} is gone from this Hermes core")
    # dict keyed by Platform / route name, or the wizard's list of {"key": ...} rows.
    keys = {row["key"] if isinstance(row, dict) else getattr(row, "value", row) for row in table}
    assert "yuanbao" not in keys
