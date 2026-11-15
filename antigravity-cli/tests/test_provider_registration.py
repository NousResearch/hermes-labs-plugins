"""Registration-level checks for the Antigravity CLI provider plugin.

No subprocess and no network: these assert the contract a Hermes core must keep for the plugin to
stay wired (profile registration, alias resolution, ACPClient class attributes, catalog hygiene).
A core change that renames ``agent.acp_client.ACPClient``, drops ``external_process`` fields, or
changes alias resolution fails here before a user does.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from providers import get_provider_profile
from providers.base import ProviderProfile

PLUGIN_DIR = Path(__file__).resolve().parents[1]


def load_plugin():
    """Import the plugin the way provider discovery does: by file, at module level."""
    spec = importlib.util.spec_from_file_location(
        "antigravity_cli_provider_plugin", PLUGIN_DIR / "__init__.py"
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def profile(name: str) -> ProviderProfile:
    found = get_provider_profile(name)
    assert found is not None, f"provider {name!r} is not registered"
    return found


def test_profile_registers_as_external_process():
    load_plugin()

    agy = profile("agy")
    assert agy.name == "agy"
    assert agy.auth_type == "external_process"
    assert agy.api_mode == "chat_completions"
    assert agy.process_command == "agy"
    assert agy.process_args == ("acp",)
    assert agy.process_command_env_vars == ("HERMES_AGY_COMMAND", "AGY_COMMAND")
    assert agy.process_args_env_var == "HERMES_AGY_ARGS"


def test_aliases_resolve_to_the_same_profile():
    load_plugin()

    for alias in ("antigravity", "antigravity-cli"):
        assert profile(alias).name == "agy"


def test_client_is_an_acp_client_with_matching_launch_defaults():
    module = load_plugin()
    from agent.acp_client import ACPClient

    assert issubclass(module.AgyACPClient, ACPClient)
    assert module.AgyACPClient.default_command == "agy"
    assert module.AgyACPClient.default_args == ("acp",)
    assert module.AgyACPClient.command_env_vars == ("HERMES_AGY_COMMAND", "AGY_COMMAND")
    assert module.AgyACPClient.args_env_var == "HERMES_AGY_ARGS"
    assert module.AgyACPClient.placeholder_model == "agy"


def test_curated_catalog_backs_every_alias():
    module = load_plugin()
    agy = profile("agy")

    assert module.MODELS
    # A picker row needs at least one id, and the profile advertises the same curated list.
    assert set(module.MODELS) <= set(agy.fallback_models)
    assert set(agy.fallback_models) <= set(agy.static_models)
    # `/model <alias>` resolves into the curated list, never to an id the session cannot select.
    assert set(agy.model_aliases.values()) <= set(agy.fallback_models)
