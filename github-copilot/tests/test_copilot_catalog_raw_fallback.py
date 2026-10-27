"""Issue #49: a failed exchange need not prevent read-only account model discovery."""

from unittest.mock import call, patch

import pytest

from github_copilot_plugin.copilot import catalog, credentials
from github_copilot_plugin.copilot.activation import copilot_user_flow

_PAYLOAD = {"data": [
    {"id": "future-chat-model", "capabilities": {"type": "chat"}},
    {"id": "embedding-model", "capabilities": {"type": "embeddings"}},
]}


def _catalog_accepting(raw_token):
    def account_catalog(url, *, timeout, headers):
        if headers.get("Authorization") != f"Bearer {raw_token}":
            raise ValueError("HTTP 401")
        return _PAYLOAD
    return account_catalog


@pytest.mark.parametrize("exchange_error", [ValueError("HTTP 404"), TimeoutError("offline")])
def test_failed_exchange_falls_back_to_raw_catalog_token(exchange_error):
    """Every pool entry is still exchanged first; only then is a raw token sent to /models."""
    with copilot_user_flow(), patch.object(
        credentials, "resolve_copilot_token", return_value=("", ""),
    ), patch("agent.credential_pool.read_credential_pool", return_value=[
        {"access_token": "gho_catalog_only"},
    ]), patch.object(
        credentials, "exchange_copilot_token", side_effect=exchange_error,
    ) as exchange, patch.object(catalog, "get_json", side_effect=_catalog_accepting("gho_catalog_only")) as fetch:
        assert catalog.copilot_picker_catalog() == ["future-chat-model"]

    exchange.assert_called_once_with("gho_catalog_only")
    assert {c.args for c in fetch.call_args_list} == {(catalog.COPILOT_MODELS_URL,)}
    assert fetch.call_args_list[0].kwargs["headers"]["Authorization"] == "Bearer gho_catalog_only"


def test_raw_pool_entry_rejected_by_catalog_does_not_hide_later_raw_entry():
    with copilot_user_flow(), patch.object(
        credentials, "resolve_copilot_token", return_value=("", ""),
    ), patch("agent.credential_pool.read_credential_pool", return_value=[
        {"access_token": "gho_dead_account"},
        {"access_token": "gho_catalog_only"},
    ]), patch.object(
        credentials, "exchange_copilot_token", side_effect=ValueError("HTTP 404"),
    ) as exchange, patch.object(catalog, "get_json", side_effect=_catalog_accepting("gho_catalog_only")) as fetch:
        assert catalog.copilot_picker_catalog() == ["future-chat-model"]

    assert exchange.call_args_list == [call("gho_dead_account"), call("gho_catalog_only")]
    assert {c.args for c in fetch.call_args_list} == {(catalog.COPILOT_MODELS_URL,)}
    auths = [c.kwargs["headers"].get("Authorization") for c in fetch.call_args_list]
    assert auths[0] == "Bearer gho_dead_account"
    assert auths[-1] == "Bearer gho_catalog_only"


def test_invalid_pool_tokens_do_not_hide_a_later_valid_account():
    with patch.object(credentials, "resolve_copilot_token", return_value=("", "")), patch(
        "agent.credential_pool.read_credential_pool", return_value=[
            None, {"access_token": ""}, {"access_token": "***"},
            {"access_token": "gho_valid_account"},
        ],
    ), patch.object(
        credentials, "exchange_copilot_token", return_value=("tid_exchanged", 1234567890.0, None),
    ) as exchange:
        assert catalog._resolve_copilot_catalog_api_key() == "tid_exchanged"
    exchange.assert_called_once_with("gho_valid_account")
