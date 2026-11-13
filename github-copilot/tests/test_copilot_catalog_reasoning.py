"""Reasoning capabilities must reach both Copilot request profiles."""

import pytest

from github_copilot_plugin.copilot import catalog, copilot


@pytest.fixture
def live_catalog(monkeypatch):
    rows = [
        {"id": model, "capabilities": {"supports": {"reasoning_effort": ["low", "high"]}}}
        for model in ("claude-opus-5.5", "gemini-3.1-pro", "gpt-6.1")
    ]
    monkeypatch.setattr(catalog, "_resolve_copilot_catalog_api_key", lambda: "stub-token")
    monkeypatch.setattr(catalog, "fetch_github_model_catalog", lambda api_key=None: rows)
    return rows


@pytest.mark.parametrize("model", ["claude-opus-5.5", "gemini-3.1-pro", "gpt-6.1"])
def test_catalog_efforts_without_explicit_catalog(live_catalog, model):
    assert catalog.github_model_reasoning_efforts(model) == ["low", "high"]


def test_chat_extras_clamps_catalog_effort(live_catalog):
    extra, top = copilot.build_api_kwargs_extras(
        model="claude-opus-5.5", reasoning_config={"effort": "xhigh"}, supports_reasoning=True)
    assert (extra, top) == ({"reasoning": {"effort": "high"}}, {})


def test_responses_clamps_catalog_effort(live_catalog):
    assert copilot.reasoning_payload("gpt-6.1", {"effort": "xhigh"},
                                     base_url="https://api.githubcopilot.com") == {"effort": "high"}


def test_offline_falls_back_without_raising(monkeypatch):
    monkeypatch.setattr(catalog, "_resolve_copilot_catalog_api_key", lambda: "stub-token")
    def offline(api_key=None):
        raise OSError("offline")
    monkeypatch.setattr(catalog, "fetch_github_model_catalog", offline)
    assert catalog.github_model_reasoning_efforts("gpt-5.4") == ["minimal", "low", "medium", "high"]
    assert catalog.github_model_reasoning_efforts("claude-opus-5.5") == []
