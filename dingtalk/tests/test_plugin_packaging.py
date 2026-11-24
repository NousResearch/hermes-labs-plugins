"""Standalone-plugin contracts: the registry entry, the env bridge, the local strings, the manifest, and
the inert guard on cores that still bundle DingTalk."""

import importlib
import logging
import types
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_CORE_LANGUAGES = ("af", "ar", "de", "en", "es", "fr", "ga", "hu", "it", "ja", "ko", "pt", "ru", "tr", "uk", "zh-hant", "zh")


class _Ctx:
    def __init__(self):
        self.kwargs = None

    def register_platform(self, **kwargs):
        self.kwargs = kwargs


def _fake_core(monkeypatch, root):
    register_mod = importlib.import_module("dingtalk_plugin.register")

    (root / "plugins" / "platforms").mkdir(parents=True, exist_ok=True)
    packages = {"plugins.platforms": types.SimpleNamespace(__path__=[str(root / "plugins" / "platforms")])}
    monkeypatch.setattr(register_mod.importlib, "import_module", packages.__getitem__)
    return register_mod


def test_registers_the_dingtalk_platform(monkeypatch):
    register_mod = importlib.import_module("dingtalk_plugin.register")
    from dingtalk_plugin.adapter import DingTalkAdapter

    monkeypatch.setattr(register_mod, "core_ships_dingtalk", lambda: False)
    ctx = _Ctx()
    register_mod.register(ctx)
    kw = ctx.kwargs
    assert kw["name"] == "dingtalk" and kw["label"] == "DingTalk" and kw["adapter_factory"] is DingTalkAdapter
    assert kw["allowed_users_env"] == "DINGTALK_ALLOWED_USERS" and kw["allow_all_env"] == "DINGTALK_ALLOW_ALL_USERS"
    assert kw["cron_deliver_env_var"] == "DINGTALK_HOME_CHANNEL"
    assert kw["standalone_send"].text_direct is True
    assert "env_overrides" not in kw


@pytest.mark.plugin_registry
def test_registry_serves_the_plugin_adapter():
    from gateway.platform_registry import platform_registry
    from dingtalk_plugin.adapter import DingTalkAdapter

    assert platform_registry.get("dingtalk").adapter_factory is DingTalkAdapter


def test_env_enablement_seeds_credentials_and_home_channel(monkeypatch):
    from dingtalk_plugin.gateway_env import env_enablement

    assert env_enablement() == {}
    monkeypatch.setenv("DINGTALK_CLIENT_ID", "cid")
    assert env_enablement() == {}
    monkeypatch.setenv("DINGTALK_CLIENT_SECRET", "csec")
    assert env_enablement() == {"client_id": "cid", "client_secret": "csec"}
    monkeypatch.setenv("DINGTALK_HOME_CHANNEL", "cidHOME")
    monkeypatch.setenv("DINGTALK_HOME_CHANNEL_NAME", "Ops")
    assert env_enablement()["home_channel"] == {"chat_id": "cidHOME", "name": "Ops"}


@pytest.mark.plugin_registry
def test_gateway_config_enables_dingtalk_from_env(monkeypatch):
    """The real config loader turns Client ID + Secret in env into an enabled ``platforms.dingtalk``."""
    from gateway.config import Platform, load_gateway_config

    monkeypatch.setenv("DINGTALK_CLIENT_ID", "cid")
    monkeypatch.setenv("DINGTALK_CLIENT_SECRET", "csec")
    monkeypatch.setenv("DINGTALK_HOME_CHANNEL", "cidHOME")
    pconf = load_gateway_config().platforms.get(Platform("dingtalk"))
    assert pconf is not None and pconf.enabled
    assert pconf.extra["client_id"] == "cid" and pconf.extra["client_secret"] == "csec"
    assert pconf.home_channel is not None and pconf.home_channel.chat_id == "cidHOME"


def test_strings_cover_every_core_language():
    from dingtalk_plugin._i18n import _MESSAGES

    assert set(_MESSAGES) == set(_CORE_LANGUAGES)
    for lang in _CORE_LANGUAGES:
        assert _MESSAGES[lang]["platform.dingtalk.emotion.thinking"].startswith("🤔")
        assert _MESSAGES[lang]["platform.dingtalk.emotion.done"].startswith("🥳")


@pytest.mark.parametrize("lang, thinking", [("en", "🤔Thinking"), ("zh-CN", "🤔思考中"), ("de", "🤔Denke nach"), ("xx", "🤔Thinking")])
def test_strings_follow_the_profile_language(monkeypatch, lang, thinking):
    from dingtalk_plugin._i18n import t

    monkeypatch.setenv("HERMES_LANGUAGE", lang)
    assert t("platform.dingtalk.emotion.thinking") == thinking


def test_manifest_name_and_optional_env():
    lines = (_ROOT / "plugin.yaml").read_text(encoding="utf-8").splitlines()
    assert "name: dingtalk" in lines and "kind: platform" in lines
    optional = {line.split(":", 1)[1].strip() for line in lines[lines.index("optional_env:"):]
                if line.startswith("  - name:")}
    assert {"DINGTALK_ALLOW_ALL_USERS", "DINGTALK_REQUIRE_MENTION", "DINGTALK_MENTION_PATTERNS",
            "DINGTALK_FREE_RESPONSE_CHATS", "DINGTALK_ALLOWED_CHATS"} <= optional


def test_inert_when_core_still_ships_dingtalk(monkeypatch, caplog):
    register_mod = importlib.import_module("dingtalk_plugin.register")

    monkeypatch.setattr(register_mod, "core_ships_dingtalk", lambda: True)
    ctx = _Ctx()
    with caplog.at_level(logging.INFO, logger=register_mod.logger.name):
        register_mod.register(ctx)
    assert ctx.kwargs is None
    assert [r.getMessage() for r in caplog.records if "stays inactive" in r.getMessage()] == [
        "dingtalk: this Hermes core still ships DingTalk built in; the plugin stays inactive until core drops it"]


def test_core_ships_dingtalk_detects_the_bundled_package(tmp_path, monkeypatch):
    (tmp_path / "plugins" / "platforms" / "dingtalk").mkdir(parents=True)
    (tmp_path / "plugins" / "platforms" / "dingtalk" / "__init__.py").write_text("")
    assert _fake_core(monkeypatch, tmp_path).core_ships_dingtalk() is True


def test_core_ships_dingtalk_ignores_a_leftover_cache_dir(tmp_path, monkeypatch):
    (tmp_path / "plugins" / "platforms" / "dingtalk" / "__pycache__").mkdir(parents=True)
    assert _fake_core(monkeypatch, tmp_path).core_ships_dingtalk() is False
