"""DingTalk cases that lived in Hermes core's shared test files until DingTalk left core, ported to the
plugin package (standalone send, allowlists, env-only credentials, the nested allowlist bridge, and the
multiplexed secondary-profile scope)."""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent import secret_scope as ss
from gateway.config import Platform, PlatformConfig


def _send(extra, chat_id, message):
    from dingtalk_plugin.adapter import _standalone_send
    return asyncio.run(_standalone_send(SimpleNamespace(token=None, extra=extra or {}), chat_id, message))


def _httpx_resp(json_data=None):
    resp = MagicMock()
    resp.status_code = 200
    resp.json = MagicMock(return_value=json_data or {"errcode": 0, "errmsg": "ok"})
    resp.raise_for_status = MagicMock()
    return resp


def _httpx_client(resp):
    client = AsyncMock()
    client.post = AsyncMock(return_value=resp)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx, client


class TestStandaloneSend:
    def test_success(self):
        ctx, client = _httpx_client(_httpx_resp())
        with patch("httpx.AsyncClient", return_value=ctx):
            result = _send({"webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=abc"}, "ignored", "hello dingtalk")
        assert result == {"success": True, "platform": "dingtalk", "chat_id": "ignored"}
        args = client.post.await_args
        assert args[0][0] == "https://oapi.dingtalk.com/robot/send?access_token=abc"
        assert args[1]["json"] == {"msgtype": "text", "text": {"content": "hello dingtalk"}}

    def test_http_error_redacts_access_token_in_exception_text(self):
        robot_credential = "robot-credential-123456789"
        resp = _httpx_resp()
        resp.raise_for_status = MagicMock(side_effect=Exception(
            f"POST https://oapi.dingtalk.com/robot/send?access_token={robot_credential} returned 401"))
        ctx, _ = _httpx_client(resp)
        with patch("httpx.AsyncClient", return_value=ctx):
            result = _send({"webhook_url": f"https://oapi.dingtalk.com/robot/send?access_token={robot_credential}"}, "ch", "hi")
        assert robot_credential not in result["error"]
        assert "access_token=***" in result["error"]

    def test_env_var_fallback(self):
        ctx, client = _httpx_client(_httpx_resp())
        with patch("httpx.AsyncClient", return_value=ctx), \
                patch.dict(os.environ, {"DINGTALK_WEBHOOK_URL": "https://oapi.dingtalk.com/robot/send?access_token=env"}):
            result = _send({}, "ch", "hi")
        assert result["success"] is True
        assert "access_token=env" in client.post.await_args[0][0]


def _bare_adapter(extra=None):
    from dingtalk_plugin.adapter import DingTalkAdapter

    adapter = object.__new__(DingTalkAdapter)
    adapter.platform = Platform("dingtalk")
    adapter.config = PlatformConfig(enabled=True, extra=dict(extra or {}))
    return adapter


class TestAllowedChats:
    def test_empty_is_no_restriction(self):
        assert _bare_adapter()._dingtalk_allowed_chats() == set()

    def test_list_form(self):
        assert _bare_adapter({"allowed_chats": ["cidABC", "cidDEF"]})._dingtalk_allowed_chats() == {"cidABC", "cidDEF"}


BRACKET_LIST = '["-100","-200"]'


def test_bracket_list_strings_are_decoded():
    """``hermes config set KEY '["-100","-200"]'`` wrote the literal string; comma-splitting it would
    yield one bogus entry and lock out every allowlisted chat/user."""
    from dingtalk_plugin.adapter import DingTalkAdapter

    adapter = DingTalkAdapter(PlatformConfig(enabled=True, extra={"allowed_chats": BRACKET_LIST, "allowed_users": '["Alice"]'}))
    assert adapter._dingtalk_allowed_chats() == {"-100", "-200"}
    assert adapter._allowed_users == {"alice"}


def test_malformed_json_stays_on_the_comma_split_path():
    assert _bare_adapter({"allowed_chats": "[not-json"})._dingtalk_allowed_chats() == {"[not-json"}


@pytest.mark.plugin_registry
def test_env_only_credentials_count_as_connected(monkeypatch):
    """Env vars without extras: the platform is connected before any env->extra seeding has run."""
    from gateway.config import GatewayConfig

    monkeypatch.setenv("DINGTALK_CLIENT_ID", "env_cid")
    monkeypatch.setenv("DINGTALK_CLIENT_SECRET", "env_sec")
    config = GatewayConfig(platforms={Platform("dingtalk"): PlatformConfig(enabled=True, extra={})})
    assert Platform("dingtalk") in config.get_connected_platforms()


def test_nested_gateway_platforms_allowed_users_bridge_to_env(monkeypatch):
    """``gateway.platforms.dingtalk.extra.allowed_users`` (the documented config.yaml form) must reach
    DINGTALK_ALLOWED_USERS: gateway authz only consults the env var."""
    from dingtalk_plugin.adapter import _apply_yaml_config

    monkeypatch.delenv("DINGTALK_ALLOWED_USERS", raising=False)
    yaml_cfg = {"gateway": {"platforms": {"dingtalk": {"enabled": True, "extra": {"allowed_users": ["user-id-1", "user-id-2"]}}}}}
    extra = _apply_yaml_config(yaml_cfg, {})
    assert (extra or {}).get("allowed_users") == ["user-id-1", "user-id-2"]
    assert os.environ.get("DINGTALK_ALLOWED_USERS") == "user-id-1,user-id-2"


@pytest.mark.plugin_registry
def test_nested_allowed_users_reach_env_through_load_gateway_config(_isolated_hermes_home, monkeypatch):
    """Same bridge, end to end: the real config loader dispatches the plugin's ``apply_yaml_config_fn``."""
    from gateway.config import load_gateway_config

    monkeypatch.delenv("DINGTALK_ALLOWED_USERS", raising=False)
    (_isolated_hermes_home / "config.yaml").write_text(
        "gateway:\n  platforms:\n    dingtalk:\n      enabled: true\n      extra:\n"
        "        allowed_users:\n          - user-id-1\n          - user-id-2\n", encoding="utf-8")
    config = load_gateway_config()
    assert config.platforms[Platform("dingtalk")].extra["allowed_users"] == ["user-id-1", "user-id-2"]
    assert os.environ.get("DINGTALK_ALLOWED_USERS") == "user-id-1,user-id-2"


DEFAULT_ENV = {
    "DINGTALK_CLIENT_ID": "default-ding-id", "DINGTALK_CLIENT_SECRET": "default-ding-secret",
    "DINGTALK_WEBHOOK_URL": "https://oapi.dingtalk.com/robot/send?access_token=DEFAULT",
    "DINGTALK_ALLOWED_USERS": "default-admin",
}
SECONDARY = {
    "DINGTALK_CLIENT_ID": "bot2-ding-id", "DINGTALK_CLIENT_SECRET": "bot2-ding-secret",
    "DINGTALK_WEBHOOK_URL": "https://oapi.dingtalk.com/robot/send?access_token=BOT2",
}


@pytest.fixture
def secondary_scope(monkeypatch):
    """Default profile in os.environ, multiplex on, a secondary profile's secret scope installed."""
    for key, value in DEFAULT_ENV.items():
        monkeypatch.setenv(key, value)
    ss.set_multiplex_active(True)
    token = ss.set_secret_scope(SECONDARY)
    yield
    ss.reset_secret_scope(token)
    ss.set_multiplex_active(False)


def test_client_id_follows_the_scoped_secret(secondary_scope):
    from dingtalk_plugin.adapter import _credentials

    assert _credentials(None) == (SECONDARY["DINGTALK_CLIENT_ID"], SECONDARY["DINGTALK_CLIENT_SECRET"])


def test_standalone_send_posts_to_the_scoped_robot_webhook(secondary_scope):
    ctx, client = _httpx_client(_httpx_resp())
    with patch("httpx.AsyncClient", return_value=ctx):
        result = _send({}, "c", "hi")
    assert result.get("success") is True
    assert client.post.await_args[0][0] == SECONDARY["DINGTALK_WEBHOOK_URL"]


def test_allowed_users_gate_ignores_the_default_profile_env(secondary_scope):
    """The default profile's DINGTALK_ALLOWED_USERS never answers a secondary scope's gate."""
    adapter = _bare_adapter()
    assert set(adapter._csv_setting("allowed_users", "DINGTALK_ALLOWED_USERS")) == set()
    token = ss.set_secret_scope({**SECONDARY, "DINGTALK_ALLOWED_USERS": "Bot2Admin"})
    try:
        assert {u.lower() for u in adapter._csv_setting("allowed_users", "DINGTALK_ALLOWED_USERS")} == {"bot2admin"}
    finally:
        ss.reset_secret_scope(token)
