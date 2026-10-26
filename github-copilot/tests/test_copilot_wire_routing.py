"""Catalog-aware HTTP routing and the pure provider hook (regressions for #37 and #38).

Only credentialed resolution may fetch a catalog. Agent, auxiliary and fallback hooks must
remain offline, including after another account has warmed the catalog cache.
"""

from __future__ import annotations

import pytest

from github_copilot_plugin.copilot import catalog, wire


@pytest.mark.parametrize("model, endpoints, expected", [
    ("grok-4.7", ["/responses"], "codex_responses"),
    ("new-vendor-model", ["/responses"], "codex_responses"),
    ("new-vendor-model", [" /V1/responses/ "], "codex_responses"),
    ("gpt-6-luna", ["/chat/completions"], "chat_completions"),
    ("grok-chat", ["/v1/chat/completions/"], "chat_completions"),
    ("gpt-5-mini", ["/responses"], "codex_responses"),
    ("gpt-6-luna", ["/responses", "/chat/completions"], "codex_responses"),
    ("gpt-5-mini", ["/responses", "/chat/completions"], "chat_completions"),
    ("grok-dual", ["/responses", "/chat/completions"], "chat_completions"),
    ("new-vendor-model", ["/responses", "/chat/completions"], "chat_completions"),
    ("claude-opus-4.6", ["/v1/messages", "/chat/completions"], "chat_completions"),
    ("claude-opus-4.6", ["/v1/messages"], "chat_completions"),
    ("new-vendor-model", ["ws:/responses"], "chat_completions"),
    ("new-vendor-model", ["wss:/responses"], "chat_completions"),
    ("new-vendor-model", ["https://example.test/responses"], "chat_completions"),
    ("new-vendor-model", ["ws:/responses", "/chat/completions"], "chat_completions"),
    ("new-vendor-model", ["/responses", None, 7], "codex_responses"),
    ("grok-4.7", [], "codex_responses"),
    ("grok-4.7", None, "codex_responses"),
    ("grok-4.7", "/chat/completions", "codex_responses"),
    ("gpt-5-mini", [], "chat_completions"),
])
def test_catalog_http_endpoints_override_family_defaults(model, endpoints, expected):
    rows = [{"id": model, "supported_endpoints": endpoints}]

    assert wire.model_api_mode(model, catalog=rows) == expected


@pytest.mark.parametrize("model, expected", [
    ("grok-4.7", "codex_responses"),
    ("x-ai/grok-4.7", "codex_responses"),
    ("xai/grok-4.7", "codex_responses"),
    ("github-copilot/grok-4.7", "codex_responses"),
    ("grok-code-fast-1", "codex_responses"),
    ("grokfoo-1", "chat_completions"),
    ("tenant/custom/grok-4.7", "chat_completions"),
    ("openai/gpt-6-luna", "codex_responses"),
    ("gpt-5-mini", "chat_completions"),
    ("claude-opus-4.6", "chat_completions"),
    ("gemini-3.1-pro-preview", "chat_completions"),
    ("new-vendor-model", "chat_completions"),
    ("", "chat_completions"),
])
def test_keyless_hook_normalizes_ids_without_catalog_io(monkeypatch, model, expected):
    from providers import get_provider_profile

    def forbidden(*args, **kwargs):
        pytest.fail("the keyless provider hook must not fetch a catalog")

    monkeypatch.setattr(catalog, "fetch_github_model_catalog", forbidden)
    profile = get_provider_profile("copilot")

    assert profile.api_mode_for_model(model) == expected
    assert profile.api_mode_for_model(model, api_key="") == expected


@pytest.mark.parametrize("rows, expected", [
    ([{"id": "grok-4.7", "supported_endpoints": ["/chat/completions"]}], "chat_completions"),
    ([{"id": "grok-4.7", "supported_endpoints": ["/responses"]}], "codex_responses"),
    (None, "codex_responses"),
])
def test_credentialed_hook_fetches_once_including_on_failure(monkeypatch, rows, expected):
    from providers import get_provider_profile

    calls = []

    def fetch(*, api_key):
        calls.append(api_key)
        return rows

    monkeypatch.setattr(catalog, "fetch_github_model_catalog", fetch)

    assert get_provider_profile("copilot").api_mode_for_model(
        "x-ai/grok-4.7", api_key="account-a") == expected
    assert calls == ["account-a"]


def test_catalog_verdict_uses_the_resolving_accounts_key(monkeypatch):
    from providers import get_provider_profile

    def fetch(*, api_key):
        endpoints = ["/responses"] if api_key == "account-a" else ["/chat/completions"]
        return [{"id": "new-vendor-model", "supported_endpoints": endpoints}]

    monkeypatch.setattr(catalog, "fetch_github_model_catalog", fetch)
    profile = get_provider_profile("copilot")

    assert profile.api_mode_for_model("new-vendor-model", api_key="account-a") == "codex_responses"
    assert profile.api_mode_for_model("new-vendor-model", api_key="account-b") == "chat_completions"
    assert profile.api_mode_for_model("new-vendor-model") == "chat_completions"


def test_a_keyless_hook_never_borrows_another_accounts_cached_catalog(monkeypatch):
    from providers import get_provider_profile

    monkeypatch.setattr(catalog, "_github_model_catalog_cache", [
        {"id": "grok-4.7", "supported_endpoints": ["/chat/completions"]}])
    monkeypatch.setattr(catalog, "_github_model_catalog_cache_key", "other-account")

    assert get_provider_profile("copilot").api_mode_for_model("grok-4.7") == "codex_responses"


@pytest.mark.parametrize("model, catalog_id", [
    ("x-ai/grok-4.7", "grok-4.7"),
    ("github-copilot/grok-4.7", "grok-4.7"),
    ("tenant/custom/model", "tenant/custom/model"),
])
def test_catalog_lookup_matches_the_normalized_id(model, catalog_id):
    rows = [{"id": catalog_id, "supported_endpoints": ["/responses"]}]

    assert wire.model_api_mode(model, catalog=rows) == "codex_responses"
