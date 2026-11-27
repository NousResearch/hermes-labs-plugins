"""The plugin must opt Copilot into core's relay-aware catalog discovery.

Core treats a configured ``model.base_url`` that differs from the profile's own
URL as a relay and skips the provider's live catalog hook unless the provider is
in ``_RELAY_AWARE_CATALOG_FETCHERS``. Copilot plan hosts (e.g.
``api.individual.githubcopilot.com``) are legitimate first-party endpoints, so
without this opt-in the picker is stuck with the stale curated list forever.
"""

from __future__ import annotations


def test_copilot_is_relay_aware_after_plugin_import():
    import hermes_cli.models as models
    from conftest import PLUGIN

    assert PLUGIN.ACTIVE
    assert "copilot" in models._RELAY_AWARE_CATALOG_FETCHERS


def test_live_catalog_hook_runs_with_a_copilot_plan_host(monkeypatch):
    """A configured plan host must not degrade the picker to its curated fallback."""
    from conftest import PLUGIN

    assert PLUGIN.ACTIVE
    from hermes_cli.config import save_config

    save_config({"model": {
        "provider": "copilot",
        "default": "mai-code-2-flash",
        "base_url": "https://api.individual.githubcopilot.com",
    }})

    from github_copilot_plugin.copilot import catalog

    calls: list[str] = []

    def fake_fetch(api_key=None, timeout=5.0):
        calls.append("called")
        return ["gpt-6.2-sol", "claude-fable-5.2"]

    monkeypatch.setattr(catalog, "_resolve_copilot_catalog_api_key", lambda: "tid=test")
    monkeypatch.setattr(catalog, "_fetch_github_models", fake_fetch)

    from hermes_cli.models import provider_model_ids

    models = provider_model_ids("copilot", force_refresh=True)
    assert calls == ["called"]
    assert "gpt-6.2-sol" in models
