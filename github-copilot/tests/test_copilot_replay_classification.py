"""Copilot connection-bound Responses items are not expired credentials."""

import httpx
import openai
import pytest

from agent.error_classifier import FailoverReason, classify_api_error
from providers import get_provider_profile


REPLAY_MESSAGE = "Input item does not belong to this connection"


def api_error(status, message):
    body = {"error": {"message": message}} if message is not None else {"error": {}}
    response = httpx.Response(status, json=body, request=httpx.Request("POST", "https://api.githubcopilot.com/responses"))
    return openai.APIStatusError(f"Error code: {status} - {body}", response=response, body=body)


def test_connection_bound_replay_401_is_not_auth_or_failover():
    verdict = classify_api_error(api_error(401, REPLAY_MESSAGE), provider="copilot", model="test-model")
    assert verdict.reason is FailoverReason.invalid_encrypted_content
    assert verdict.retryable is False
    assert verdict.should_rotate_credential is False
    assert verdict.should_fallback is False


@pytest.mark.parametrize("status,message", [
    (401, "The access token has expired"),
    (401, "Input item belongs to another connection"),
    (401, None),
    (400, REPLAY_MESSAGE),
    (403, REPLAY_MESSAGE),
    (500, REPLAY_MESSAGE),
])
def test_nonmatching_errors_are_not_classified_as_invalid_encrypted_content(status, message):
    profile = get_provider_profile("copilot")
    assert profile.classify_api_error(
        api_error(status, message), status_code=status, error_code="", message=(message or "").lower(),
        body={}, model="test-model",
    ) is None


def test_real_token_expiry_remains_auth():
    verdict = classify_api_error(api_error(401, "The access token has expired"), provider="copilot")
    assert verdict.reason is FailoverReason.auth
    assert verdict.should_rotate_credential is True


def test_replay_401_never_triggers_broad_stale_credential_predicate():
    profile = get_provider_profile("copilot")
    message = f"Error code: 400 model_not_available_for_integrator; {REPLAY_MESSAGE}"
    assert profile.is_stale_credential_error(status_code=401, message=message) is False


def test_genuine_stale_integrator_error_still_triggers_reexchange():
    profile = get_provider_profile("copilot")
    assert profile.is_stale_credential_error(status_code=400, message="model_not_available_for_integrator") is True


@pytest.mark.parametrize("status,message", [
    (401, "The access token has expired"),
    (401, "Input item belongs to another connection"),
    (401, None),
    (400, REPLAY_MESSAGE),
    (403, REPLAY_MESSAGE),
    (500, REPLAY_MESSAGE),
])
def test_nonmatching_errors_remain_on_generic_classifier(status, message):
    verdict = classify_api_error(api_error(status, message), provider="copilot")
    assert verdict.reason is not FailoverReason.invalid_encrypted_content


def test_malformed_body_does_not_match():
    response = httpx.Response(401, text="{not-json", request=httpx.Request("POST", "https://api.githubcopilot.com/responses"))
    error = openai.APIStatusError("Error code: 401", response=response, body="{not-json")
    verdict = classify_api_error(error, provider="copilot")
    assert verdict.reason is FailoverReason.auth


def test_phrase_in_non_message_field_does_not_match():
    body = {"error": {"code": REPLAY_MESSAGE, "message": "Token expired"}}
    response = httpx.Response(401, json=body, request=httpx.Request("POST", "https://api.githubcopilot.com/responses"))
    error = openai.APIStatusError(f"Error code: 401 - {body}", response=response, body=body)
    assert classify_api_error(error, provider="copilot").reason is FailoverReason.auth


def test_replay_classification_is_scoped_to_copilot_provider():
    verdict = classify_api_error(api_error(401, REPLAY_MESSAGE), provider="openai")
    assert verdict.reason is FailoverReason.auth


@pytest.mark.parametrize("base_url,expected", [
    ("https://api.githubcopilot.com", "copilot"),
    ("https://api.business.githubcopilot.com", "copilot"),
    ("https://relay.example.test", None),
    ("https://api.githubcopilot.com.example.test", None),
])
def test_custom_route_refresh_profile_respects_host(base_url, expected):
    from types import SimpleNamespace
    from agent.conversation_loop import _credential_refresh_profile

    profile = _credential_refresh_profile(SimpleNamespace(provider="custom", base_url=base_url))
    assert getattr(profile, "name", None) == expected
