"""Antigravity CLI (``agy``) model provider — Google Antigravity models over ACP.

``agy acp`` is an Agent Client Protocol stdio server, so the provider is an
``external_process`` profile whose ``create_client`` returns an
:class:`agent.acp_client.ACPClient` subclass: Hermes spawns one short-lived
``agy acp`` session per request, applies the requested model over ACP
(``session/set_config_option``, else ``session/set_model``), and forwards its
tools as a prompt-level bridge.

Login belongs to the CLI — ``agy`` signs in through the browser and keeps the
credential in the OS keyring — so there is no API key for Hermes to store.
Install the Antigravity CLI, run ``agy`` once to sign in, then:

    hermes -m gemini-3.8-flash --provider agy -z "hello"
"""

from __future__ import annotations

from agent.acp_client import ACPClient
from providers import register_provider
from providers.base import ProviderProfile

# Launch overrides, checked in order. These names are executable authority: Hermes'
# generic env writers refuse them, so set them in .env or the shell.
COMMAND_ENV_VARS = ("HERMES_AGY_COMMAND", "AGY_COMMAND")
ARGS_ENV_VAR = "HERMES_AGY_ARGS"
MARKER_BASE_URL = "acp://agy"
PLACEHOLDER_MODEL = "agy"

# Curated catalog: the ids `agy acp` advertises in `session/new` for a signed-in
# account (agy 1.5.x). The live session stays authoritative; this list is the
# picker fallback when the probe cannot run. The shell wrapper's `agy models`
# lists the same families with explicit thinking tiers
# (gemini-3.8-flash-high/medium/low) — over ACP the tier is a separate session
# config option, so the base id is what gets sent.
MODELS = (
    "gemini-3.8-flash",
    "claude-sonnet-4-6",
    "claude-opus-4-6-thinking",
    "gpt-oss-120b-medium",
)

# `/model <alias>` convenience, resolved before core's fuzzy model guessing.
MODEL_ALIASES = {
    "flash": "gemini-3.8-flash",
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-6-thinking",
    "gpt-oss": "gpt-oss-120b-medium",
}


class AgyACPClient(ACPClient):
    """``ACPClient`` tuned for the Antigravity CLI's ACP stdio server."""

    default_command = "agy"
    default_args = ("acp",)
    command_env_vars = COMMAND_ENV_VARS
    args_env_var = ARGS_ENV_VAR
    marker_base_url = MARKER_BASE_URL
    placeholder_model = PLACEHOLDER_MODEL
    label = "Antigravity CLI"
    missing_command_hint = (
        "Install the Antigravity CLI, then run `agy` once to sign in. If `agy` works in your "
        "shell but not here, this process' PATH does not include ~/.local/bin — pin the absolute "
        "path with HERMES_AGY_COMMAND in the profile's .env."
    )
    auth_required_hint = "Run `agy` to sign in to your Google Antigravity account, then retry."
    unsupported_transport_hint = (
        "This `agy` build has no `acp` subcommand; update the Antigravity CLI "
        "(`agy update`) and retry."
    )


class AgyProfile(ProviderProfile):
    """External-process profile: the CLI owns its own login and model catalog."""

    def create_client(self, **client_kwargs):
        return AgyACPClient(**client_kwargs)


register_provider(
    AgyProfile(
        name="agy",
        aliases=("antigravity", "antigravity-cli"),
        display_name="Antigravity CLI (agy)",
        description="Google Antigravity models via the local `agy` CLI over ACP",
        signup_url="https://antigravity.google/",
        api_mode="chat_completions",
        auth_type="external_process",
        env_vars=(),
        base_url=MARKER_BASE_URL,
        process_command="agy",
        process_args=("acp",),
        process_command_env_vars=COMMAND_ENV_VARS,
        process_args_env_var=ARGS_ENV_VAR,
        fallback_models=MODELS,
        static_models=MODELS,
        model_aliases=MODEL_ALIASES,
        default_aux_model="gemini-3.8-flash",
        supports_model_listing=True,
        supports_health_check=True,
    )
)
