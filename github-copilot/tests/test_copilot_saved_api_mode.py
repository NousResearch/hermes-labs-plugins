"""A ``model.api_mode`` saved by an earlier setup must not strand Claude on a wrong wire or split one session
across wires.

Core honors a persisted ``model.api_mode`` over the profile's per-model wire, except that it rescues a stale
``codex_responses`` (saved when a GPT-5+ model was picked) to ``chat_completions`` when the profile answers
``chat_completions`` for the target model (hermes_cli/runtime_provider.py ``_profile_runtime_api_mode``,
#46527). If Claude answered ``anthropic_messages`` there, the rescue would not fire and Claude would resolve
to ``codex_responses`` (Copilot rejects Claude on Responses). So while a legacy saved mode is present, Claude
answers ``chat_completions``, the behavior before the /v1/messages route, until the user removes it
(``hermes model`` or ``hermes config unset model.api_mode``).
"""

from __future__ import annotations

import json

import pytest

import providers as _providers
from hermes_cli import runtime_provider as rp
from providers.base import ProviderProfile

_providers.get_provider_profile("copilot")  # imports the copilot plugin package
from github_copilot_plugin.copilot import credentials as copilot_credentials
from github_copilot_plugin.copilot import wire as copilot_wire

needs_seam = pytest.mark.skipif(not hasattr(ProviderProfile, "anthropic_auth_style"),
                                reason="core lacks ProviderProfile.anthropic_auth_style")


def _write_config(hermes_home, api_mode: str | None) -> None:
    from hermes_cli import config as cfg

    lines = ["model:", "  provider: copilot", "  default: gpt-5.4", "  base_url: https://api.githubcopilot.com"]
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
    monkeypatch.setattr(rp, "resolve_provider", lambda *a, **k: "copilot")


def _resolved_mode(target_model: str) -> str:
    return rp.resolve_runtime_provider(requested="copilot", target_model=target_model)["api_mode"]


@needs_seam
def test_saved_codex_responses_does_not_strand_a_claude_target_on_responses(hermes_home, offline_copilot):
    _write_config(hermes_home, "codex_responses")
    assert _resolved_mode("claude-sonnet-5.5") == "chat_completions"  # core's rescue, as before this route
    assert _resolved_mode("gpt-5.4") == "codex_responses"


@needs_seam
def test_saved_codex_responses_keeps_claude_off_messages_on_every_path(hermes_home):
    """``/model`` and resume re-derive the wire from the profile (``model_derived_api_mode``) and agree with
    startup, so one session never flips between wires."""
    from hermes_cli.model_switch import model_derived_api_mode

    _write_config(hermes_home, "codex_responses")
    assert model_derived_api_mode("copilot", "claude-sonnet-5.5") == "chat_completions"
    assert model_derived_api_mode("copilot", "gpt-5.4") == "codex_responses"


@needs_seam
def test_removing_the_saved_api_mode_moves_claude_to_messages(hermes_home, offline_copilot, monkeypatch):
    """What ``hermes config unset model.api_mode`` (or re-running ``hermes model``) does."""
    from github_copilot_plugin.copilot import catalog

    monkeypatch.setattr(catalog, "fetch_github_model_catalog", lambda **k: [])
    _write_config(hermes_home, "codex_responses")
    assert _resolved_mode("claude-sonnet-5.5") == "chat_completions"
    _write_config(hermes_home, None)
    assert _resolved_mode("claude-sonnet-5.5") == "anthropic_messages"


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


@needs_seam
def test_without_a_saved_api_mode_claude_resolves_to_messages(hermes_home, offline_copilot, monkeypatch):
    from github_copilot_plugin.copilot import catalog

    _write_config(hermes_home, None)
    monkeypatch.setattr(catalog, "fetch_github_model_catalog", lambda **k: [])
    assert _resolved_mode("claude-sonnet-5.5") == "anthropic_messages"
    assert _resolved_mode("gpt-5.4") == "codex_responses"


@needs_seam
def test_saved_chat_completions_is_honored_as_before(hermes_home, offline_copilot):
    _write_config(hermes_home, "chat_completions")
    assert _resolved_mode("claude-sonnet-5.5") == "chat_completions"


def test_before_the_messages_route_a_saved_codex_responses_was_rescued_for_claude(
        hermes_home, offline_copilot, monkeypatch):
    """The baseline this route must keep: without the core seam Claude answers ``chat_completions`` and core
    rescues a saved ``codex_responses`` to it."""
    monkeypatch.setattr(copilot_wire, "core_serves_anthropic_bearer", lambda: False)
    _write_config(hermes_home, "codex_responses")
    assert _resolved_mode("claude-sonnet-5.5") == "chat_completions"
