"""Exercise the registered routing hook through core's real primary, aux and fallback paths."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from github_copilot_plugin.copilot import catalog, credentials

TOKEN = "tid=offline-routing-test"
BASE_URL = "https://api.githubcopilot.com"


@pytest.fixture
def offline_credentials(monkeypatch):
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "gho_offlineRoutingTest000000000000000000")
    monkeypatch.setattr(credentials, "get_copilot_api_token", lambda raw: (TOKEN, BASE_URL))


@pytest.mark.parametrize("provider", ["copilot", "github-copilot"])
@pytest.mark.parametrize("model, endpoints, expected", [
    ("grok-4.7", ["/responses"], "codex_responses"),
    ("new-vendor-model", ["/responses"], "codex_responses"),
    ("gpt-6-luna", ["/chat/completions"], "chat_completions"),
])
def test_runtime_resolves_the_target_model_with_its_credential(
    hermes_home, monkeypatch, offline_credentials, provider, model, endpoints, expected,
):
    from hermes_cli import config, runtime_provider

    (hermes_home / "config.yaml").write_text(
        "model:\n  provider: copilot\n  default: claude-opus-4.6\n")
    config._LOAD_CONFIG_CACHE.clear()
    config._RAW_CONFIG_CACHE.clear()
    keys = []

    def fetch(*, api_key):
        keys.append(api_key)
        return [{"id": model, "supported_endpoints": endpoints}]

    monkeypatch.setattr(catalog, "fetch_github_model_catalog", fetch)

    runtime = runtime_provider.resolve_runtime_provider(requested=provider, target_model=model)

    assert runtime["api_mode"] == expected
    assert runtime["api_key"] == TOKEN
    assert keys == [TOKEN]


@pytest.mark.parametrize("saved_mode", ["codex_responses", "chat_completions"])
def test_saved_grok_mode_is_not_silently_changed(
    hermes_home, monkeypatch, offline_credentials, saved_mode,
):
    from hermes_cli import config, runtime_provider

    (hermes_home / "config.yaml").write_text(
        f"model:\n  provider: copilot\n  default: grok-4.7\n  api_mode: {saved_mode}\n")
    config._LOAD_CONFIG_CACHE.clear()
    config._RAW_CONFIG_CACHE.clear()
    monkeypatch.setattr(catalog, "fetch_github_model_catalog", lambda **kwargs: pytest.fail("unexpected catalog I/O"))

    runtime = runtime_provider.resolve_runtime_provider(requested="copilot", target_model="grok-4.7")

    # Core retains explicit modes; its pure compatibility check must not downgrade a valid Grok Responses mode.
    assert runtime["api_mode"] == saved_mode


@pytest.mark.parametrize("model, expected", [
    ("grok-4.7", "codex_responses"),
    ("x-ai/grok-4.7", "codex_responses"),
    ("gpt-5-mini", "chat_completions"),
    ("claude-opus-4.6", "chat_completions"),
])
def test_primary_and_auxiliary_use_the_registered_keyless_hook(monkeypatch, model, expected):
    from agent.auxiliary_client import CodexAuxiliaryClient, resolve_provider_client
    from run_agent import AIAgent

    monkeypatch.setattr(catalog, "fetch_github_model_catalog", lambda **kwargs: None)

    agent = AIAgent(
        provider="copilot", model=model, api_key=TOKEN, base_url=BASE_URL, quiet_mode=True,
        skip_context_files=True, skip_memory=True, enabled_toolsets=[],
    )
    client, resolved_model = resolve_provider_client(
        "copilot", model, explicit_api_key=TOKEN, explicit_base_url=BASE_URL,
    )

    assert agent.api_mode == expected
    assert resolved_model == model.split("/", 1)[-1]
    assert isinstance(client, CodexAuxiliaryClient) is (expected == "codex_responses")


@pytest.mark.parametrize("explicit_mode, expected", [
    (None, "codex_responses"),
    ("chat_completions", "chat_completions"),
])
def test_running_fallback_recomputes_grok_wire_but_keeps_explicit_overrides(
    monkeypatch, offline_credentials, explicit_mode, expected,
):
    from run_agent import AIAgent

    monkeypatch.setattr(catalog, "fetch_github_model_catalog", lambda **kwargs: None)
    fallback = {"provider": "copilot", "model": "grok-4.7", "api_key": TOKEN, "base_url": BASE_URL}
    if explicit_mode is not None:
        fallback["api_mode"] = explicit_mode
    agent = AIAgent(
        provider="custom", model="primary-model", api_key="primary-key",
        base_url="https://primary.example.test/v1", quiet_mode=True,
        skip_context_files=True, skip_memory=True, enabled_toolsets=[], fallback_model=[fallback],
    )

    assert agent._try_activate_fallback() is True
    assert agent.provider == "copilot"
    assert agent.model == "grok-4.7"
    assert agent.api_mode == expected


def test_init_fallback_uses_its_own_provider_model_hook(monkeypatch):
    from agent.agent_init_fallback import recompute_init_fallback_api_mode
    from run_agent import AIAgent

    monkeypatch.setattr(catalog, "fetch_github_model_catalog", lambda **kwargs: None)
    agent = AIAgent(
        provider="copilot", model="grok-4.7", api_key=TOKEN, base_url=BASE_URL, quiet_mode=True,
        skip_context_files=True, skip_memory=True, enabled_toolsets=[], api_mode="chat_completions",
    )

    recompute_init_fallback_api_mode(agent, agent.client)

    assert agent.api_mode == "codex_responses"


def test_grok_primary_turn_posts_responses_with_real_sdk(monkeypatch):
    import json

    import httpx
    import openai

    from run_agent import AIAgent

    paths = []

    def answer(request):
        paths.append(request.url.path)
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(400, json={"error": {"message": "Responses-only model"}})
        assert request.url.path.endswith("/responses")
        body = json.loads(request.content)
        assert body["model"] == "grok-4.7"
        response = {
            "id": "resp_offline", "object": "response", "created_at": 0, "status": "completed",
            "model": "grok-4.7", "output": [{"id": "msg_offline", "type": "message",
                "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": "OK", "annotations": []}]}],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        }
        events = [
            {"type": "response.output_text.delta", "item_id": "msg_offline", "output_index": 0,
             "content_index": 0, "delta": "OK"},
            {"type": "response.completed", "response": response},
        ]
        sse = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    def offline_openai(**kwargs):
        return openai.OpenAI(**{**kwargs, "http_client": httpx.Client(transport=httpx.MockTransport(answer))})

    monkeypatch.setattr(catalog, "fetch_github_model_catalog", lambda **kwargs: None)
    with patch("agent.process_bootstrap.OpenAI", side_effect=offline_openai):
        agent = AIAgent(
            provider="copilot", model="grok-4.7", api_key=TOKEN, base_url=BASE_URL, quiet_mode=True,
            skip_context_files=True, skip_memory=True, enabled_toolsets=[],
        )
        result = agent.chat("Reply with exactly OK")

    assert paths and all(path.endswith("/responses") for path in paths)
    assert result == "OK"
