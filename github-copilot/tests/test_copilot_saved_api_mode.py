"""Which wire Claude gets when ``config.yaml`` carries a saved ``model.api_mode``.

Core with the saved-api_mode fix (``hermes_cli.runtime_provider.persisted_api_mode_applies``) honors a saved
``model.api_mode`` for the saved ``model.default`` only; every other model of the provider gets the profile's
per-model wire, and a ``/model --global`` / Desktop pick no longer saves one. On that core:

- a value saved for another model never pins Claude (or GPT) to a wrong wire;
- a legacy value saved for a Claude default (``chat_completions`` from an earlier setup, or ``codex_responses``
  that core rescues to ``chat_completions``) keeps that one model on ``/chat/completions`` on every path, so a
  session never mixes wires; other Claude models use ``/v1/messages``;
- an ``anthropic_messages`` saved for a Claude default by an older build leaves GPT targets on their own wire.

An older core honors a saved mode for every model and saves the picked model's wire, so a saved
``anthropic_messages`` would send GPT targets (delegation, cron, auxiliary tasks) to ``/v1/messages``. There the
plugin keeps Claude on ``/chat/completions`` and so never produces that value.
"""

from __future__ import annotations

import json

import pytest

import providers as _providers
from hermes_cli import runtime_provider as rp
from providers.base import ProviderProfile

_providers.get_provider_profile("copilot")  # imports the copilot plugin package
from github_copilot_plugin.copilot import catalog as copilot_catalog
from github_copilot_plugin.copilot import credentials as copilot_credentials
from github_copilot_plugin.copilot import wire as copilot_wire

_HAS_SEAM = hasattr(ProviderProfile, "anthropic_auth_style")
_HAS_FIX = callable(getattr(rp, "persisted_api_mode_applies", None))
needs_seam = pytest.mark.skipif(not _HAS_SEAM, reason="core lacks ProviderProfile.anthropic_auth_style")
needs_route = pytest.mark.skipif(not (_HAS_SEAM and _HAS_FIX),
                                 reason="core lacks the Anthropic auth seam or the saved-api_mode fix")


def _write_config(hermes_home, api_mode: str | None, default: str = "gpt-5.4") -> None:
    from hermes_cli import config as cfg

    lines = ["model:", "  provider: copilot", f"  default: {default}", "  base_url: https://api.githubcopilot.com"]
    if api_mode:
        lines.append(f"  api_mode: {api_mode}")
    (hermes_home / "config.yaml").write_text("\n".join(lines) + "\n")
    (hermes_home / "auth.json").write_text(json.dumps({"version": 1, "credential_pool": {}}))
    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()


@pytest.fixture
def offline_copilot(monkeypatch):
    monkeypatch.setattr(copilot_credentials, "resolve_copilot_token", lambda: ("ghu_raw_gh_token", "GH_TOKEN"))
    monkeypatch.setattr(copilot_credentials, "get_copilot_api_token",
                        lambda tok: ("tid=exchanged;exp=1", "https://api.githubcopilot.com"))
    monkeypatch.setattr(copilot_catalog, "fetch_github_model_catalog", lambda **k: [])
    monkeypatch.setattr(rp, "resolve_provider", lambda *a, **k: "copilot")


def _resolved_mode(target_model: str | None = None) -> str:
    return rp.resolve_runtime_provider(requested="copilot", target_model=target_model)["api_mode"]


def _derived_mode(model: str) -> str | None:
    """What ``/model``, resume and (on an older core) a saved selection use."""
    from hermes_cli.model_switch import model_derived_api_mode

    return model_derived_api_mode("copilot", model)


# ── core with the saved-api_mode fix ──


@needs_route
def test_without_a_saved_api_mode_claude_resolves_to_messages(hermes_home, offline_copilot):
    _write_config(hermes_home, None)
    assert _resolved_mode("claude-sonnet-5.5") == "anthropic_messages"
    assert _resolved_mode("gpt-5.4") == "codex_responses"


@needs_route
def test_a_value_saved_for_a_gpt_default_does_not_pin_claude(hermes_home, offline_copilot):
    _write_config(hermes_home, "codex_responses")
    assert _resolved_mode() == _resolved_mode("gpt-5.4") == "codex_responses"
    assert _resolved_mode("claude-sonnet-5.5") == _derived_mode("claude-sonnet-5.5") == "anthropic_messages"


@needs_route
def test_saved_anthropic_messages_for_a_claude_default_does_not_break_gpt_targets(hermes_home, offline_copilot):
    """What an older build saved on ``/model --global claude-...``: GPT targets keep their own wire."""
    _write_config(hermes_home, "anthropic_messages", default="claude-opus-5.5")
    assert _resolved_mode() == "anthropic_messages"
    assert _resolved_mode("gpt-5.4") == "codex_responses"
    assert _resolved_mode("gpt-4.1") == "chat_completions"
    assert _resolved_mode("gemini-3-pro-preview") == "chat_completions"


@needs_route
@pytest.mark.parametrize("saved", ["chat_completions", "codex_responses"])
def test_a_legacy_value_saved_for_a_claude_default_keeps_that_model_on_chat_completions(
        hermes_home, offline_copilot, saved):
    """Startup honors (or rescues) the saved value for the default, and ``/model`` / resume ask the profile:
    both answer ``chat_completions`` for that model, so one session never mixes wires."""
    _write_config(hermes_home, saved, default="claude-sonnet-5.5")
    assert _resolved_mode() == _resolved_mode("claude-sonnet-5.5") == "chat_completions"
    assert _derived_mode("claude-sonnet-5.5") == "chat_completions"
    assert _resolved_mode("claude-opus-5.5") == _derived_mode("claude-opus-5.5") == "anthropic_messages"
    assert _resolved_mode("gpt-5.4") == "codex_responses"


@needs_route
def test_removing_the_saved_api_mode_moves_the_claude_default_to_messages(hermes_home, offline_copilot):
    """What ``hermes config unset model.api_mode`` (or re-running ``hermes model``) does."""
    _write_config(hermes_home, "chat_completions", default="claude-sonnet-5.5")
    assert _resolved_mode() == "chat_completions"
    _write_config(hermes_home, None, default="claude-sonnet-5.5")
    assert _resolved_mode() == "anthropic_messages"


@needs_route
@pytest.mark.parametrize("default, target", [
    ("claude-sonnet-4-6", "claude-sonnet-4.6"),  # dashed alias vs the dotted id /model and resume normalize to
    ("anthropic/claude-sonnet-4.6", "claude-sonnet-4.6"),
    ("claude-sonnet-4.6", "anthropic/claude-sonnet-4-6"),
])
def test_a_legacy_pin_matches_the_default_across_id_spellings(hermes_home, default, target):
    """Startup passes the saved id as written; ``/model`` and resume pass the normalized one. The pin compares
    both after the plugin's own (catalog-free) normalization, so the wire never flips mid-session."""
    _write_config(hermes_home, "chat_completions", default=default)
    assert copilot_wire._legacy_api_mode_pins(default) is True
    assert copilot_wire._legacy_api_mode_pins(target) is True
    assert copilot_wire._legacy_api_mode_pins("claude-opus-5.5") is False


@needs_route
@pytest.mark.parametrize("model_block", [
    "model:\n  provider: copilot\n  model: claude-sonnet-5.5\n  api_mode: chat_completions\n",  # legacy alias
    "model:\n  default:\n    model: claude-sonnet-5.5\n    provider: copilot\n  api_mode: chat_completions\n",
])
def test_a_legacy_pin_reads_the_default_the_way_core_does(hermes_home, model_block):
    from hermes_cli import config as cfg

    (hermes_home / "config.yaml").write_text(model_block)
    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    assert copilot_wire._legacy_api_mode_pins("claude-sonnet-5.5") is True
    assert copilot_wire._legacy_api_mode_pins("claude-opus-5.5") is False


@needs_route
def test_a_global_claude_pick_saves_no_api_mode(hermes_home, offline_copilot):
    from hermes_cli.model_switch import ModelSwitchResult, model_selection_config_updates

    result = ModelSwitchResult(success=True, new_model="claude-opus-5.5", target_provider="copilot",
                               base_url="https://api.githubcopilot.com", api_mode="anthropic_messages",
                               is_global=True)
    assert model_selection_config_updates(result, {"provider": "copilot", "api_mode": "chat_completions"})[
        "api_mode"] is None


# ── a core without the saved-api_mode fix ──


@needs_seam
def test_without_the_core_fix_claude_never_answers_anthropic_messages(hermes_home, monkeypatch):
    """An older core saves the derived wire on ``/model --global`` and honors it for every model: the plugin
    keeps Claude on ``chat_completions`` there, so ``anthropic_messages`` is never saved and GPT targets keep
    working."""
    monkeypatch.setattr(copilot_wire, "core_scopes_saved_api_mode", lambda: False)
    _write_config(hermes_home, None, default="claude-opus-5.5")
    assert _derived_mode("claude-opus-5.5") == "chat_completions"
    assert _derived_mode("gpt-5.4") == "codex_responses"
    profile = _providers.get_provider_profile("copilot")
    assert profile.api_mode_for_model("claude-opus-5.5") == "chat_completions"


def test_core_fix_detection_follows_the_core_helper(monkeypatch):
    assert copilot_wire.core_scopes_saved_api_mode() is _HAS_FIX
    monkeypatch.delattr(rp, "persisted_api_mode_applies", raising=False)
    assert copilot_wire.core_scopes_saved_api_mode() is False


def test_without_the_core_fix_a_saved_anthropic_messages_is_reported_once(hermes_home, monkeypatch, caplog):
    """An older core applies a saved ``anthropic_messages`` (written by an earlier build of this route) to every
    Copilot model, which no profile answer can override: the plugin says once how to remove it."""
    import logging

    monkeypatch.setattr(copilot_wire, "core_scopes_saved_api_mode", lambda: False)
    monkeypatch.setattr(copilot_wire, "_WARNED_SAVED_MESSAGES_WIRE", set())
    _write_config(hermes_home, "anthropic_messages", default="claude-opus-5.5")
    with caplog.at_level(logging.WARNING, logger=copilot_wire.logger.name):
        assert copilot_wire._api_mode_for_id("gpt-4.1") == "chat_completions"
        assert copilot_wire._api_mode_for_id("claude-opus-5.5") == "chat_completions"
    warnings = [r for r in caplog.records if "hermes config unset model.api_mode" in r.getMessage()]
    assert len(warnings) == 1


@needs_route
def test_with_the_core_fix_a_saved_anthropic_messages_is_not_reported(hermes_home, caplog):
    import logging

    _write_config(hermes_home, "anthropic_messages", default="claude-opus-5.5")
    with caplog.at_level(logging.WARNING, logger=copilot_wire.logger.name):
        copilot_wire._api_mode_for_id("gpt-4.1")
    assert not [r for r in caplog.records if "model.api_mode" in r.getMessage()]


def test_before_the_messages_route_a_saved_codex_responses_was_rescued_for_claude(
        hermes_home, offline_copilot, monkeypatch):
    """The baseline this route must keep: without the core seam Claude answers ``chat_completions`` and core
    rescues a saved ``codex_responses`` to it."""
    monkeypatch.setattr(copilot_wire, "core_serves_anthropic_bearer", lambda: False)
    _write_config(hermes_home, "codex_responses", default="claude-sonnet-5.5")
    assert _resolved_mode("claude-sonnet-5.5") == "chat_completions"


# ── setup ──


def test_a_managed_or_default_api_mode_is_never_popped_by_setup(copilot_profile_setup, hermes_home, monkeypatch):
    """Setup removes only the key in the user's own config.yaml (``read_raw_config``): an ``api_mode`` the
    merged config gets from elsewhere (managed overlay, defaults) is never popped and re-saved."""
    from hermes_cli import config as cfg

    (hermes_home / "config.yaml").write_text("model:\n  provider: copilot\n  default: gpt-4.1\n")
    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    merged = {"model": {"provider": "copilot", "default": "gpt-4.1", "api_mode": "chat_completions"}}
    monkeypatch.setattr(cfg, "load_config", lambda: merged)
    saves = []
    monkeypatch.setattr(cfg, "save_config", lambda *a, **k: saves.append(a))
    copilot_profile_setup._drop_saved_api_mode()
    assert saves == []


@pytest.fixture
def copilot_profile_setup():
    import sys

    profile = _providers.get_provider_profile("copilot")
    return sys.modules[f"{type(profile).__module__}.setup"]
