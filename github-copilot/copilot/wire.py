"""GitHub Copilot wire quirks: per-model api_mode, editor/initiator/vision headers, max-tokens param.

Fills the generic ProviderProfile wire seams (``providers/base.py`` L1 block). Every hook here answers
from its arguments alone, except ``api_mode_for_model``, which reads ``COPILOT_API_BASE_URL`` and the
cached config (``model.base_url`` / ``model.api_mode``) and, with an ``api_key``, the account's cached
live catalog to normalize the model id first.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from .helpers import base_url_host_matches, base_url_hostname

# Copilot's own API host family (individual, business and enterprise subdomains).
_COPILOT_HOST = "githubcopilot.com"
# Hosts whose first call of a user turn carries ``x-initiator: user`` (Hermes core's set). Every other host,
# including the plan hosts the token exchange hands out (api.individual./api.business./api.enterprise.),
# keeps the client default ``agent`` by design; see README § Premium requests.
_INITIATOR_HOSTS = ("api.githubcopilot.com", "models.github.ai")


def is_copilot_host(base_url: str) -> bool:
    return base_url_host_matches(str(base_url or ""), _COPILOT_HOST)


def requires_responses_api(model_id: str) -> bool:
    """opencode's ``shouldUseCopilotResponsesApi``: GPT-5+ uses the Responses API except
    ``gpt-5-mini``; non-GPT models (Claude, Gemini, ...) use Chat Completions."""
    match = re.match(r"^gpt-(\d+)", model_id)
    return bool(match) and int(match.group(1)) >= 5 and not model_id.startswith("gpt-5-mini")


def core_serves_anthropic_bearer() -> bool:
    """Whether the running core asks profiles for the Anthropic client's auth style
    (``ProviderProfile.anthropic_auth_style``). Without that seam core would send the Copilot token
    as ``x-api-key`` on /v1/messages and get a 401, so Claude stays on Chat Completions there."""
    from providers.base import ProviderProfile

    return hasattr(ProviderProfile, "anthropic_auth_style")


# Claude variants whose hyphenated id (core's Anthropic wire spelling: ``claude-opus-4.8-fast`` ->
# ``claude-opus-4-8-fast``) Copilot's /v1/messages rejects with ``model_not_supported``; they stay on Chat
# Completions until core can keep a provider's dotted ids on the Anthropic wire.
_MESSAGES_UNSUPPORTED_SUFFIXES = ("-fast",)


def _copilot_model_config() -> dict:
    """config.yaml's ``model`` section when its provider is a Copilot id, else ``{}`` (cached, read-only)."""
    from hermes_cli.config import load_config_readonly

    from .activation import COPILOT_PROVIDER_IDS

    model = (load_config_readonly() or {}).get("model")
    if isinstance(model, dict) and str(model.get("provider") or "").strip().lower() in COPILOT_PROVIDER_IDS:
        return model
    return {}


def _configured_base_url() -> str:
    """The Copilot API base URL the user pointed Hermes at: ``COPILOT_API_BASE_URL``, else a Copilot
    ``model.base_url`` in config.yaml, else the default (the token exchange only ever hands out Copilot hosts)."""
    from .catalog import COPILOT_BASE_URL

    try:
        from agent.secret_scope import get_secret

        env_url = (get_secret("COPILOT_API_BASE_URL") or "").strip()
        if env_url:
            return env_url
        return str(_copilot_model_config().get("base_url") or "").strip() or COPILOT_BASE_URL
    except Exception:  # an unreadable config/secret store keeps the default host
        pass
    return COPILOT_BASE_URL


# Wires earlier versions of this plugin's setup saved as ``model.api_mode`` (``model_api_mode`` of the model
# picked then). Core honors a saved mode over the profile, except that it rescues ``codex_responses`` to
# ``chat_completions`` only when the profile answers ``chat_completions`` for the target model.
_LEGACY_SAVED_API_MODES = frozenset({"chat_completions", "codex_responses"})


def _legacy_api_mode_saved() -> bool:
    """Whether the user's config still pins a Copilot ``model.api_mode`` an earlier setup saved. While it
    does, Claude answers ``chat_completions`` (the wire before /v1/messages) on every path: startup honors
    the saved mode (or rescues a saved ``codex_responses`` to ``chat_completions``), and ``/model`` derives
    the same answer, so one session never mixes wires. ``hermes model`` or ``hermes config unset
    model.api_mode`` removes it."""
    try:
        return str(_copilot_model_config().get("api_mode") or "").strip().lower() in _LEGACY_SAVED_API_MODES
    except Exception:  # an unreadable config keeps the new route
        return False


def uses_messages_api(model_id: str) -> bool:
    """Claude models go to Copilot's Anthropic ``/v1/messages`` endpoint when core can drive it, the
    configured base URL is a Copilot host (a proxy would get core's generic ``x-api-key`` auth there) and
    no legacy ``model.api_mode`` is saved: it honors ``cache_control`` TTLs and reports cache writes, which
    ``/chat/completions`` does not."""
    bare = str(model_id or "").strip().lower().rsplit("/", 1)[-1]  # ``anthropic/claude-...`` too
    return (bare.startswith("claude-") and not bare.endswith(_MESSAGES_UNSUPPORTED_SUFFIXES)
            and core_serves_anthropic_bearer() and is_copilot_host(_configured_base_url())
            and not _legacy_api_mode_saved())


def _api_mode_for_id(model_id: str) -> str:
    if requires_responses_api(model_id):
        return "codex_responses"
    if uses_messages_api(model_id):
        return "anthropic_messages"
    return "chat_completions"


def model_api_mode(
    model_id: Optional[str], *, catalog: Optional[list[dict[str, Any]]] = None,
    api_key: Optional[str] = None) -> str:
    """API mode for a Copilot model from the id pattern (opencode's approach): GPT-5+ on Responses,
    Claude on Anthropic Messages (when core has the auth seam), everything else on Chat Completions."""
    from .catalog import fetch_github_model_catalog, normalize_copilot_model_id

    if catalog is None and api_key:  # fetch once so normalize + endpoint check share it
        catalog = fetch_github_model_catalog(api_key=api_key)
    normalized = normalize_copilot_model_id(model_id, catalog=catalog, api_key=api_key)
    return _api_mode_for_id(normalized) if normalized else "chat_completions"


def editor_headers(*, is_vision: bool = False) -> dict[str, str]:
    from .credentials import copilot_request_headers

    return copilot_request_headers(is_agent_turn=True, is_vision=is_vision)


class CopilotWireMixin:
    """ProviderProfile wire hooks for the Copilot API."""

    def api_mode_for_model(self, model: str, *, api_key: str | None = None) -> str | None:
        if api_key is None:
            return _api_mode_for_id(model)
        return model_api_mode(model, api_key=api_key)

    def anthropic_auth_style(self, *, base_url: str) -> str | None:
        # Copilot's /v1/messages takes the exchanged token as Authorization: Bearer, not x-api-key; it
        # needs no Copilot-Vision-Request (only the OpenAI-wire image client asks has_images=True).
        return "bearer" if is_copilot_host(base_url) else None

    def client_headers(
        self, *, base_url: str, api_key: str = "", has_images: bool = False,
    ) -> dict[str, str] | None:
        return editor_headers(is_vision=has_images) if is_copilot_host(base_url) else None

    def request_headers(self, *, base_url: str, model: str = "", user_initiated: bool = False) -> dict[str, str]:
        # First call of a user turn says ``user``; follow-ups keep the client default ``x-initiator: agent``.
        if user_initiated and any(base_url_host_matches(base_url, h) for h in _INITIATOR_HOSTS):
            return {"x-initiator": "user"}
        return {}

    def max_tokens_param(self, *, base_url: str, model: str = "") -> str | None:
        host = base_url_hostname(str(base_url or "")) or ""
        return "max_completion_tokens" if host.endswith("." + _COPILOT_HOST) else None
