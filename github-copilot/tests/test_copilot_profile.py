"""Unit tests for the Copilot provider profile's reasoning-effort wiring.

GitHub Copilot serves different models with different supported reasoning-effort
sets (the live ``/models`` catalog reports them per model). The profile must
forward the requested effort when the catalog lists it as supported, and only
downgrade to the nearest weaker supported level when it does not, rather than
unconditionally collapsing ``xhigh`` to ``high`` (which silently capped models
that actually support the higher level).

These tests pin that contract without going live, by stubbing the catalog
lookup ``github_model_reasoning_efforts`` or passing it an explicit ``catalog=``.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def copilot_profile():
    """Resolve the registered Copilot profile.

    Importing ``model_tools`` triggers plugin discovery, which registers the
    Copilot profile. Going through ``get_provider_profile`` keeps the test
    honest: if the registered class is ever swapped for a plain
    ``ProviderProfile`` the assertions below collapse.
    """
    import model_tools
    import providers

    profile = providers.get_provider_profile("copilot")
    assert profile is not None, "copilot provider profile must be registered"
    return profile


def _patch_efforts(monkeypatch, efforts):
    """Stub the catalog lookup the profile and main-agent path call for supported efforts."""
    import providers

    providers.get_provider_profile("copilot")  # imports the copilot plugin package (catalog module)
    from github_copilot_plugin.copilot import catalog as copilot_catalog

    monkeypatch.setattr(copilot_catalog, "github_model_reasoning_efforts", lambda model: list(efforts))


class TestCopilotReasoningEffortClamp:
    def test_supported_effort_forwarded_verbatim(self, copilot_profile, monkeypatch):
        """xhigh is forwarded unchanged as top-level reasoning_effort when the catalog lists it."""
        _patch_efforts(monkeypatch, ["minimal", "low", "medium", "high", "xhigh"])
        extra_body, top_level = copilot_profile.build_api_kwargs_extras(
            model="gpt-5.5",
            reasoning_config={"effort": "xhigh"},
            supports_reasoning=True,
        )
        assert extra_body == {}
        assert top_level == {"reasoning_effort": "xhigh"}

    def test_xhigh_downgrades_to_high_when_unsupported(self, copilot_profile, monkeypatch):
        """A model whose catalog lacks xhigh gets the nearest weaker level."""
        _patch_efforts(monkeypatch, ["low", "medium", "high"])
        extra_body, top_level = copilot_profile.build_api_kwargs_extras(
            model="gemini-4-carbon",
            reasoning_config={"effort": "max"},
            supports_reasoning=True,
        )
        assert extra_body == {}
        assert top_level == {"reasoning_effort": "high"}

    def test_minimal_downgrades_to_low_when_unsupported(self, copilot_profile, monkeypatch):
        _patch_efforts(monkeypatch, ["low", "medium", "high"])
        extra_body, top_level = copilot_profile.build_api_kwargs_extras(
            model="o-series-model",
            reasoning_config={"effort": "minimal"},
            supports_reasoning=True,
        )
        assert extra_body == {}
        assert top_level == {"reasoning_effort": "low"}

    def test_unsupported_effort_falls_back_to_medium(self, copilot_profile, monkeypatch):
        """An effort not in the set, with no specific rule, falls to medium."""
        _patch_efforts(monkeypatch, ["low", "medium", "high"])
        extra_body, top_level = copilot_profile.build_api_kwargs_extras(
            model="some-model",
            reasoning_config={"effort": "garbage"},
            supports_reasoning=True,
        )
        assert extra_body == {}
        assert top_level == {"reasoning_effort": "medium"}

    def test_falls_back_to_first_supported_when_no_medium(self, copilot_profile, monkeypatch):
        """If medium isn't supported either, pick the first supported level."""
        _patch_efforts(monkeypatch, ["low", "high"])
        extra_body, top_level = copilot_profile.build_api_kwargs_extras(
            model="weird-model",
            reasoning_config={"effort": "xhigh"},
            supports_reasoning=True,
        )
        # xhigh not supported, high IS supported → high wins via the xhigh rule.
        assert extra_body == {}
        assert top_level == {"reasoning_effort": "high"}

    def test_first_supported_when_no_rule_matches(self, copilot_profile, monkeypatch):
        _patch_efforts(monkeypatch, ["low", "high"])
        extra_body, top_level = copilot_profile.build_api_kwargs_extras(
            model="weird-model",
            reasoning_config={"effort": "garbage"},
            supports_reasoning=True,
        )
        assert extra_body == {}
        assert top_level == {"reasoning_effort": "low"}

    def test_disabled_reasoning_emits_none_only_when_supported(self, copilot_profile, monkeypatch):
        """Auxiliary calls with enabled=False omit reasoning_effort when 'none' is unsupported
        (Gemini 4 / Claude 5 400 on 'none') and emit 'none' when the model supports it."""
        _patch_efforts(monkeypatch, ["low", "medium", "high"])
        assert copilot_profile.build_api_kwargs_extras(
            model="gemini-4-carbon",
            reasoning_config={"enabled": False, "effort": "high"},
            supports_reasoning=True,
        ) == ({}, {})

        _patch_efforts(monkeypatch, ["none", "low", "medium", "high"])
        assert copilot_profile.build_api_kwargs_extras(
            model="gpt-5.6-sol",
            reasoning_config={"enabled": False, "effort": "high"},
            supports_reasoning=True,
        ) == ({}, {"reasoning_effort": "none"})


class TestCopilotOfflineAstraEfforts:
    """With no live catalog entry, offline ladders cover Astra, Gemini 3.8/4 and Claude 5.x,
    while a structured catalog entry keeps precedence over the offline fallback."""

    @pytest.mark.parametrize("model, expected", [
        ("gpt-6-astra", ["low", "medium", "high", "xhigh", "max"]),
        ("openai/gpt-6-astra", ["low", "medium", "high", "xhigh", "max"]),
        ("gpt-6-astra-pro", []),  # speed-tier / unknown suffixes stay off the Astra ladder
        ("gemini-4-carbon", ["low", "medium", "high"]),
        ("gemini-4-argon", ["low", "medium", "high"]),
        ("gemini-3.8-flash", ["low", "medium", "high"]),
        ("claude-opus-5.5", ["low", "medium", "high", "xhigh", "max"]),
        ("claude-sonnet-5.5", ["low", "medium", "high", "xhigh", "max"]),
        ("claude-haiku-5.5", ["low", "medium", "high", "xhigh", "max"]),
    ])
    def test_offline_fallback_uses_exact_astra_slugs(self, model, expected):
        from github_copilot_plugin.copilot.catalog import github_model_reasoning_efforts

        assert github_model_reasoning_efforts(model, catalog=[]) == expected

    def test_structured_catalog_beats_offline_fallback(self):
        from github_copilot_plugin.copilot.catalog import github_model_reasoning_efforts

        catalog = [{"id": "gpt-6-astra", "capabilities": {
            "type": "chat", "supports": {"reasoning_effort": ["low", "high"]}}}]
        assert github_model_reasoning_efforts("gpt-6-astra", catalog=catalog) == ["low", "high"]


class TestCopilotLiveCatalogReasoningResolution:
    """Profile hooks omit catalog/api_key; github_model_reasoning_efforts resolves the active
    Copilot credential when allowed, stays gated off for non-Copilot homes, and scopes cache by key."""

    def test_resolves_from_live_catalog_when_copilot_chosen(self, hermes_home, monkeypatch):
        (hermes_home / "config.yaml").write_text("model:\n  provider: copilot\n  default: custom-reasoner\n")
        from github_copilot_plugin.copilot import catalog as copilot_catalog

        monkeypatch.setattr(copilot_catalog, "_resolve_copilot_catalog_api_key", lambda: "tok-a")
        monkeypatch.setattr(
            copilot_catalog,
            "fetch_github_model_catalog",
            lambda api_key=None, timeout=5.0: [
                {"id": "custom-reasoner", "capabilities": {"supports": {"reasoning_effort": ["low", "high"]}}}
            ],
        )
        assert copilot_catalog.github_model_reasoning_efforts("custom-reasoner") == ["low", "high"]

    def test_ambient_token_on_non_copilot_home_never_fetches_catalog(self, hermes_home, monkeypatch):
        (hermes_home / "config.yaml").write_text("model:\n  provider: nous\n  default: Hermes-4-405B\n")
        monkeypatch.setenv("GH_TOKEN", "gho_ambient_token_1234567890")
        from github_copilot_plugin.copilot import catalog as copilot_catalog

        called = []
        monkeypatch.setattr(copilot_catalog, "_resolve_copilot_catalog_api_key",
                            lambda: called.append("resolve") or "tok")
        assert copilot_catalog.github_model_reasoning_efforts("custom-reasoner") == []
        assert called == []

    def test_two_credentials_do_not_share_cached_reasoning_efforts(self, monkeypatch):
        from github_copilot_plugin.copilot import catalog as copilot_catalog

        by_token = {
            "tok-enterprise": [{"id": "model-x", "capabilities": {"type": "chat", "supports": {
                "reasoning_effort": ["low", "medium", "high", "max"]}}}],
            "tok-individual": [{"id": "model-x", "capabilities": {"type": "chat", "supports": {
                "reasoning_effort": ["low", "medium"]}}}],
        }
        monkeypatch.setattr(copilot_catalog, "get_json", lambda url, *, timeout, headers=None: {
            "data": by_token[headers["Authorization"].removeprefix("Bearer ")]
        })
        assert copilot_catalog.github_model_reasoning_efforts("model-x", api_key="tok-enterprise") == [
            "low", "medium", "high", "max"
        ]
        assert copilot_catalog.github_model_reasoning_efforts("model-x", api_key="tok-individual") == [
            "low", "medium"
        ]


@pytest.mark.parametrize("effort, expected", [
    ("xhigh", "high"), ("max", "high"), ("ultra", "high"), ("minimal", "minimal"),
    ("garbage", "medium"), (" HIGH ", "high"),
])
def test_main_agent_github_clamp_never_escalates(monkeypatch, effort, expected):
    """The main-agent GitHub path shares the profile's clamp and sends top-level reasoning_effort
    (never extra_body.reasoning, which Copilot /chat/completions ignores)."""
    from conftest import offline_chat_turn

    _patch_efforts(monkeypatch, ["minimal", "low", "medium", "high"])
    requests = offline_chat_turn("https://api.githubcopilot.com",
                                 reasoning_config={"enabled": True, "effort": effort})
    assert requests, "the turn sent no chat request"
    assert requests[0].body.get("reasoning_effort") == expected
    assert "reasoning" not in requests[0].body


def test_main_agent_gemini4_carbon_clamps_max_to_top_level_high(hermes_home):
    """End-to-end turn on gemini-4-carbon with effort='max' puts top-level reasoning_effort='high' on the wire."""
    from conftest import offline_chat_turn

    (hermes_home / "config.yaml").write_text("model:\n  provider: nous\n")  # keep offline
    requests = offline_chat_turn("https://api.enterprise.githubcopilot.com", model="gemini-4-carbon",
                                 reasoning_config={"enabled": True, "effort": "max"})
    assert requests, "the turn sent no chat request"
    assert requests[0].body.get("reasoning_effort") == "high"
    assert "reasoning" not in requests[0].body
