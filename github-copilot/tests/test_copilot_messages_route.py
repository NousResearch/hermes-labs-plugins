"""Claude on Copilot's Anthropic ``/v1/messages`` endpoint.

Copilot's ``/chat/completions`` ignores ``cache_control`` TTLs and reports no cache writes; ``/v1/messages``
honors both. Claude models route there (``api_mode`` ``anthropic_messages``) only on a core whose Anthropic
client asks the profile for its auth style (``ProviderProfile.anthropic_auth_style``) and only when the
configured base URL is a Copilot host: without the seam core would send the Copilot token as ``x-api-key``
and get a 401, and a proxy base URL would get core's generic auth, so Claude stays on Chat Completions there.
"""

from __future__ import annotations

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
    ("anthropic/claude-sonnet-5.5", "anthropic_messages"),
    ("claude-opus-4.8-fast", "chat_completions"),  # its hyphenated wire id is model_not_supported on /v1/messages
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


@needs_seam
@pytest.mark.parametrize("base_url, mode", [
    ("https://copilot-proxy.example.test", "chat_completions"),
    ("http://127.0.0.1:8080", "chat_completions"),
    ("https://api.githubcopilot.com.evil.test", "chat_completions"),
    ("https://api.business.githubcopilot.com", "anthropic_messages"),
])
def test_claude_uses_messages_only_on_a_copilot_base_url(copilot_profile, monkeypatch, base_url, mode):
    """A ``COPILOT_API_BASE_URL`` proxy keeps Claude on the Chat Completions route it already worked on."""
    monkeypatch.setenv("COPILOT_API_BASE_URL", base_url)
    assert copilot_profile.api_mode_for_model("claude-sonnet-5.5") == mode
    assert copilot_profile.api_mode_for_model("gpt-4.1") == "chat_completions"


@needs_seam
def test_a_configured_proxy_base_url_keeps_claude_on_chat_completions(copilot_profile, hermes_home):
    from hermes_cli import config as cfg

    (hermes_home / "config.yaml").write_text(
        "model:\n  provider: copilot\n  default: claude-sonnet-5.5\n  base_url: https://copilot-proxy.example.test\n")
    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    assert copilot_profile.api_mode_for_model("claude-sonnet-5.5") == "chat_completions"


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
def test_claude_turn_goes_to_v1_messages_with_bearer_auth_on_the_wire(url):
    """One real agent turn through core's Anthropic client, asserted on the HTTP request itself."""
    from conftest import offline_messages_turn

    sent = offline_messages_turn(url, model="claude-sonnet-5.5")
    assert sent, "no /v1/messages request reached the wire"
    first = sent[0]
    assert first.path == "/v1/messages"
    assert first.headers["authorization"] == "Bearer tid=offline"
    assert "x-api-key" not in first.headers
    # Core's Anthropic wire spells Claude ids with hyphens; Copilot serves that spelling (live-verified for
    # every Claude id in the catalog except claude-opus-4.8-fast, which stays on Chat Completions).
    assert first.body["model"] == "claude-sonnet-5-5"
    assert first.headers["copilot-integration-id"] == "vscode-chat"
    assert "editor-version" in first.headers
    # The api.githubcopilot.com host bills the user's first call of a turn as ``user`` (README § Premium
    # requests); the plan hosts keep the client default ``agent``.
    assert first.headers["x-initiator"] == ("user" if url == "https://api.githubcopilot.com" else "agent")
    assert "copilot-vision-request" not in first.headers  # /v1/messages needs no vision header (live-verified)


def test_setup_saves_no_api_mode_and_drops_one_an_earlier_setup_saved(copilot_profile, hermes_home, monkeypatch):
    """Older setups saved ``model.api_mode: chat_completions``, which core honors over the per-model wire."""
    import sys

    from hermes_cli import config as cfg

    setup = sys.modules[f"{type(copilot_profile).__module__}.setup"]
    (hermes_home / "config.yaml").write_text(
        "model:\n  provider: copilot\n  default: gpt-4.1\n  base_url: https://api.githubcopilot.com\n"
        "  api_mode: chat_completions\n")
    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    monkeypatch.setattr(setup, "copilot_api_key", lambda: ("tid=offline", "COPILOT_GITHUB_TOKEN"))
    monkeypatch.setattr(setup, "_copilot_catalog", lambda key: ([], [], lambda mid: mid))
    monkeypatch.setattr(setup, "_raw_model_ids", lambda key: [])
    monkeypatch.setattr(setup, "pick_model_or_prompt", lambda *a, **k: "claude-sonnet-5.5")
    setup._model_flow_copilot("gpt-4.1")
    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    model = cfg.load_config()["model"]
    assert (model["provider"], model["default"]) == ("copilot", "claude-sonnet-5.5")
    assert "api_mode" not in model
