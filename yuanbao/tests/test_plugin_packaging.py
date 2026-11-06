"""Standalone-plugin contracts: the yb_* toolset, the cached platform hint, send-target syntax, the env
bridge, the local strings, and the inert guard on cores that still bundle Yuanbao."""

import importlib
import logging
import types

import pytest

YB_TOOLS = {"yb_query_group_info", "yb_query_group_members", "yb_send_dm", "yb_search_sticker", "yb_send_sticker"}


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


def _fake_core(monkeypatch, root):
    register_mod = importlib.import_module("yuanbao_plugin.register")

    (root / "plugins" / "platforms").mkdir(parents=True, exist_ok=True)
    (root / "tools").mkdir(exist_ok=True)
    packages = {
        "plugins.platforms": types.SimpleNamespace(__path__=[str(root / "plugins" / "platforms")]),
        "tools": types.SimpleNamespace(__path__=[str(root / "tools")]),
    }
    monkeypatch.setattr(register_mod.importlib, "import_module", packages.__getitem__)
    return register_mod


def test_registers_the_yuanbao_toolset(monkeypatch):
    register_mod = importlib.import_module("yuanbao_plugin.register")

    monkeypatch.setattr(register_mod, "core_ships_yuanbao", lambda: False)
    ctx = _Ctx()
    register_mod.register(ctx)
    assert {t["name"] for t in ctx.tools} == YB_TOOLS
    assert {t["toolset"] for t in ctx.tools} == {"yuanbao"}
    assert all(t["is_async"] and callable(t["check_fn"]) for t in ctx.tools)
    assert ctx.kwargs["allowed_users_env"] == "YUANBAO_ALLOWED_USERS"
    assert ctx.kwargs["own_policy_env"] == ("YUANBAO_DM_POLICY", "YUANBAO_GROUP_POLICY")


@pytest.mark.plugin_registry
def test_tools_find_the_adapter_the_registry_created():
    """ONE ``YuanbaoAdapter`` class object: the registry's factory, the tools' lookup and the standalone
    sender all resolve it through this package, so ``get_active()`` sees the gateway's instance."""
    import yuanbao_plugin.tools as tools_mod
    from gateway.platform_registry import platform_registry
    from yuanbao_plugin.adapter import YuanbaoAdapter

    entry = platform_registry.get("yuanbao")
    assert entry.adapter_factory is YuanbaoAdapter
    sentinel = object()
    YuanbaoAdapter.set_active(sentinel)
    try:
        assert tools_mod._get_active_adapter() is sentinel
    finally:
        YuanbaoAdapter.set_active(None)


def test_registers_the_yuanbao_skill(monkeypatch):
    """Core's former optional ``yuanbao`` skill ships with the plugin (``yuanbao:yuanbao``) and documents the
    plugin's own tools."""
    register_mod = importlib.import_module("yuanbao_plugin.register")

    monkeypatch.setattr(register_mod, "core_ships_yuanbao", lambda: False)
    ctx = _Ctx()
    register_mod.register(ctx)
    [(name, path, description)] = ctx.skills
    text = path.read_text(encoding="utf-8")
    assert name == "yuanbao" and text.startswith("---\nname: yuanbao\n")
    assert f'description: "{description}"' in text
    assert {"yb_query_group_info", "yb_query_group_members", "yb_send_dm"} <= {
        tool for tool in YB_TOOLS if f"`{tool}`" in text}


def test_platform_hint_keeps_core_bytes():
    """Cached-prompt bytes: core's adapter hint only. A session note there would reach cron prompts and be
    dropped by a ``platform_hints`` replace, so the DM guidance rides on the yb_send_dm schema instead."""
    from yuanbao_plugin.register import _PLATFORM_HINT
    from yuanbao_plugin.tools import _TOOLS

    assert _PLATFORM_HINT.startswith("You are on Yuanbao (腾讯元宝), a Chinese AI assistant platform.")
    assert _PLATFORM_HINT.endswith("bare Unicode emoji is not a substitute.")
    assert len(_PLATFORM_HINT) == 715 and "running inside Yuanbao" not in _PLATFORM_HINT
    dm = next(schema for schema, _, _ in _TOOLS if schema["name"] == "yb_send_dm")["description"]
    assert "normal reply already goes to the group you are responding in" in dm


def test_send_target_syntax():
    from gateway.platforms.send_targets import NOT_EXPLICIT
    from yuanbao_plugin.register import platform_kwargs

    parse = platform_kwargs()["target_ref_syntax_fn"]
    assert parse("group:123") == ("group:123", None)
    assert parse(" direct:abc") == ("direct:abc", None)
    assert parse("456") == ("group:456", None)
    assert parse("Some Group") is NOT_EXPLICIT


def test_env_enablement_seeds_core_extras(monkeypatch):
    from yuanbao_plugin.gateway_env import env_enablement

    assert env_enablement() == {}
    monkeypatch.setenv("YUANBAO_APP_KEY", "legacy-id")
    assert env_enablement() == {}
    monkeypatch.setenv("YUANBAO_APP_SECRET", "s3cret")
    monkeypatch.setenv("YUANBAO_DM_POLICY", " AllowList ")
    monkeypatch.setenv("YUANBAO_WS_URL", "wss://127.0.0.1:9/ws")
    monkeypatch.setenv("YUANBAO_HOME_CHANNEL", "group:42")
    seed = env_enablement()
    assert seed["app_id"] == "legacy-id" and seed["app_secret"] == "s3cret"
    assert seed["dm_policy"] == "allowlist" and seed["ws_url"] == "wss://127.0.0.1:9/ws"
    assert seed["home_channel"] == {"chat_id": "group:42", "name": "Home"}
    monkeypatch.setenv("YUANBAO_APP_ID", "new-id")
    assert env_enablement()["app_id"] == "new-id"


def test_gateway_config_enables_yuanbao_from_env(monkeypatch):
    """The real config loader turns App ID + App Secret in env into an enabled ``platforms.yuanbao`` with
    core's extras and home channel."""
    from gateway.config import Platform, load_gateway_config

    monkeypatch.setenv("YUANBAO_APP_ID", "app")
    monkeypatch.setenv("YUANBAO_APP_SECRET", "secret")
    monkeypatch.setenv("YUANBAO_HOME_CHANNEL", "direct:owner")
    pconf = load_gateway_config().platforms.get(Platform("yuanbao"))
    assert pconf is not None and pconf.enabled
    assert pconf.extra["app_id"] == "app" and pconf.extra["app_secret"] == "secret"
    assert pconf.home_channel is not None and pconf.home_channel.chat_id == "direct:owner"


@pytest.mark.parametrize("lang, expected", [
    ("en", "This task is a bit involved — still working on it, please hang on..."),
    ("zh-CN", "任务有点复杂，正在努力处理中，请耐心等待..."),
    ("zh-TW", "這個任務比較複雜 — 仍在處理中，請稍候..."),
    ("xx", "This task is a bit involved — still working on it, please hang on..."),
])
def test_strings_follow_the_profile_language(monkeypatch, lang, expected):
    from yuanbao_plugin._i18n import t

    monkeypatch.setenv("HERMES_LANGUAGE", lang)
    assert t("platform.yuanbao.slow_response_notice") == expected
    assert "/new" in t("platform.yuanbao.owner_command_denied", command="/new")


def test_inert_when_core_still_ships_yuanbao(monkeypatch, caplog):
    """On a core that still bundles Yuanbao the plugin registers nothing (no shadowed yb_* tools, no replaced
    adapter) and says so once."""
    register_mod = importlib.import_module("yuanbao_plugin.register")

    monkeypatch.setattr(register_mod, "core_ships_yuanbao", lambda: True)
    ctx = _Ctx()
    with caplog.at_level(logging.INFO, logger=register_mod.logger.name):
        register_mod.register(ctx)
    assert ctx.kwargs is None and ctx.tools == [] and ctx.skills == []
    assert [r.getMessage() for r in caplog.records if "stays inactive" in r.getMessage()] == [
        "yuanbao: this Hermes core still ships Yuanbao built in; the plugin stays inactive until core drops it"]


@pytest.mark.parametrize("rel", ["plugins/platforms/yuanbao/__init__.py", "tools/yuanbao_tools.py"])
def test_core_ships_yuanbao_detects_each_bundled_path(tmp_path, monkeypatch, rel):
    (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_text("")
    assert _fake_core(monkeypatch, tmp_path).core_ships_yuanbao() is True


def test_core_ships_yuanbao_ignores_a_leftover_cache_dir(tmp_path, monkeypatch):
    """An update that deleted the bundled files can leave ``plugins/platforms/yuanbao/__pycache__``."""
    (tmp_path / "plugins" / "platforms" / "yuanbao" / "__pycache__").mkdir(parents=True)
    assert _fake_core(monkeypatch, tmp_path).core_ships_yuanbao() is False
