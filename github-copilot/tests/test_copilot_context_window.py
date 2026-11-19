"""#44: the context window, not the prompt cap, is the model's context size."""

import providers as _providers

_providers.get_provider_profile("copilot")
from github_copilot_plugin.copilot import catalog as cat  # noqa: E402


def _catalog(limits):
    return [{"id": "gpt-5.5", "capabilities": {"limits": limits}}]


def _reset():
    cat._copilot_context_cache = {}
    cat._copilot_context_cache_time = 0.0
    cat._copilot_context_cache_key = None


def test_prefers_context_window_over_prompt_cap(monkeypatch):
    _reset()
    monkeypatch.setattr(cat, "fetch_github_model_catalog", lambda **k: _catalog(
        {"max_context_window_tokens": 400000, "max_prompt_tokens": 272000, "max_output_tokens": 128000}))
    assert cat.get_copilot_model_context("gpt-5.5") == 400000


def test_falls_back_to_prompt_cap(monkeypatch):
    _reset()
    monkeypatch.setattr(cat, "fetch_github_model_catalog", lambda **k: _catalog({"max_prompt_tokens": 272000}))
    assert cat.get_copilot_model_context("gpt-5.5") == 272000


def test_invalid_window_falls_back(monkeypatch):
    _reset()
    for bad in (0, -1, "400000", None):
        _reset()
        monkeypatch.setattr(cat, "fetch_github_model_catalog", lambda **k: _catalog(
            {"max_context_window_tokens": bad, "max_prompt_tokens": 272000}))
        assert cat.get_copilot_model_context("gpt-5.5") == 272000


def test_no_limits_yields_none(monkeypatch):
    _reset()
    monkeypatch.setattr(cat, "fetch_github_model_catalog", lambda **k: _catalog({}))
    assert cat.get_copilot_model_context("gpt-5.5") is None
