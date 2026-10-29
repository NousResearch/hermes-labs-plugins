"""Claude on Copilot's Anthropic ``/v1/messages`` endpoint.

Copilot's ``/chat/completions`` ignores ``cache_control`` TTLs and reports no cache writes; ``/v1/messages``
honors both. Claude models route there (``api_mode`` ``anthropic_messages``) only on a core whose Anthropic
client asks the profile for its auth style (``ProviderProfile.anthropic_auth_style``): without it core
would send the Copilot token as ``x-api-key`` and get a 401, so Claude stays on Chat Completions.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from providers.base import ProviderProfile

_HAS_SEAM = hasattr(ProviderProfile, "anthropic_auth_style")
needs_seam = pytest.mark.skipif(not _HAS_SEAM, reason="core lacks ProviderProfile.anthropic_auth_style")


@pytest.fixture
def copilot_profile():
    import providers

    profile = providers.get_provider_profile("copilot")
    assert profile is not None
    return profile


@needs_seam
@pytest.mark.parametrize("model, mode", [
    ("claude-sonnet-5.5", "anthropic_messages"),
    ("claude-opus-4.6-1m", "anthropic_messages"),
    ("gpt-5.4", "codex_responses"),
    ("gpt-5-mini", "chat_completions"),
    ("gpt-4.1", "chat_completions"),
    ("gemini-3-pro-preview", "chat_completions"),
])
def test_api_mode_for_model(copilot_profile, model, mode):
    assert copilot_profile.api_mode_for_model(model) == mode


@needs_seam
def test_catalog_path_routes_claude_to_messages():
    from github_copilot_plugin.copilot.wire import model_api_mode

    assert model_api_mode("claude-sonnet-5.5", catalog=[]) == "anthropic_messages"


def test_claude_stays_on_chat_completions_without_the_core_seam(copilot_profile, monkeypatch):
    from github_copilot_plugin.copilot import wire

    monkeypatch.setattr(wire, "core_serves_anthropic_bearer", lambda: False)
    assert copilot_profile.api_mode_for_model("claude-sonnet-5.5") == "chat_completions"
    assert wire.model_api_mode("claude-sonnet-5.5", catalog=[]) == "chat_completions"


@pytest.mark.parametrize("url, style", [
    ("https://api.githubcopilot.com", "bearer"),
    ("https://api.individual.githubcopilot.com", "bearer"),
    ("https://api.business.githubcopilot.com", "bearer"),
    ("https://models.github.ai/inference", None),
    ("https://api.githubcopilot.com.evil.test", None),
    ("https://evil.test/api.githubcopilot.com", None),
])
def test_anthropic_auth_style_is_host_anchored(copilot_profile, url, style):
    assert copilot_profile.anthropic_auth_style(base_url=url) == style


@needs_seam
@pytest.mark.parametrize("url", ["https://api.githubcopilot.com", "https://api.individual.githubcopilot.com"])
def test_core_anthropic_client_sends_bearer_and_editor_headers(url):
    """Through core's ``build_anthropic_client``: Bearer auth, no x-api-key, Copilot editor headers."""
    from agent.anthropic_adapter import build_anthropic_client

    with patch("agent.anthropic_adapter._anthropic_sdk") as sdk:
        build_anthropic_client("tid=offline", base_url=url)
    kwargs = sdk.Anthropic.call_args[1]
    assert kwargs["auth_token"] == "tid=offline"
    assert "api_key" not in kwargs
    headers = {k.lower(): v for k, v in kwargs["default_headers"].items() if isinstance(v, str)}
    assert headers["copilot-integration-id"] == "vscode-chat"
    assert headers["x-initiator"] == "agent"
    assert "editor-version" in headers
    # /v1/messages needs no Copilot-Vision-Request: live 16px/256px PNG checks returned 200 with correct
    # colors on a client built this way.
    assert "copilot-vision-request" not in headers
