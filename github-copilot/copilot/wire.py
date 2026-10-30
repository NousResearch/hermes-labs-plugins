"""GitHub Copilot wire quirks: per-model api_mode, editor/initiator/vision headers, max-tokens param.

Fills the generic ProviderProfile wire seams (``providers/base.py`` L1 block). Every hook here answers
from its arguments alone, except ``api_mode_for_model``, which reads ``COPILOT_API_BASE_URL`` (through
``get_secret``), the cached config (``model.base_url`` / ``model.default`` / ``model.api_mode``, through
``load_config_readonly``) and the in-memory token-exchange cache (never the network or disk) and, with an
``api_key``, the account's cached live catalog to normalize the model id first.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from .helpers import base_url_host_matches, base_url_hostname

logger = logging.getLogger(__name__)

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


def core_scopes_saved_api_mode() -> bool:
    # Whether the running core applies a saved ``model.api_mode`` to the saved ``model.default`` only
    # (``hermes_cli.runtime_provider.persisted_api_mode_applies``). An older core honors a saved mode for
    # every model of the provider and saves the picked model's wire on ``/model --global`` and Desktop picks:
    # once a Claude pick saved ``anthropic_messages`` there, every GPT target (delegation, cron, auxiliary
    # tasks) would go to /v1/messages and fail, and no profile answer can undo that at runtime. So on such a
    # core Claude stays on Chat Completions and that value is never written.
    try:
        from hermes_cli import runtime_provider
    except Exception:  # health: allow BLE001 -- feature detection: no core module means no fix
        logger.debug("saved-api_mode scoping not detected", exc_info=True)
        return False
    return callable(getattr(runtime_provider, "persisted_api_mode_applies", None))


def core_serves_messages_route() -> bool:
    # Both core changes /v1/messages needs: Bearer auth for the Anthropic client and saved-api_mode scoping.
    return core_serves_anthropic_bearer() and core_scopes_saved_api_mode()


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
    """The Copilot API base URL the user pointed Hermes at: ``COPILOT_API_BASE_URL`` (through ``get_secret``),
    else a Copilot ``model.base_url`` in config.yaml (``load_config_readonly``), else the default."""
    from .catalog import COPILOT_BASE_URL

    try:
        from agent.secret_scope import get_secret

        env_url = (get_secret("COPILOT_API_BASE_URL") or "").strip()
        if env_url:
            return env_url
        return str(_copilot_model_config().get("base_url") or "").strip() or COPILOT_BASE_URL
    except Exception as exc:  # an unreadable config/secret store keeps the default host
        logger.debug("Copilot base URL lookup failed (%s); using the default host", type(exc).__name__)
    return COPILOT_BASE_URL


def _exchanged_base_urls_are_copilot_hosts() -> bool:
    """Whether the API base URL the token exchange handed out for the current GitHub token (``endpoints.api``
    / ``proxy-ep``, which ``runtime_base_url`` prefers over the configured one) is a Copilot host. Reads only
    the in-memory exchange cache; True when that token has not been exchanged in this process yet."""
    try:
        from .credentials import _jwt_cache, _token_fingerprint, resolve_copilot_token

        raw_token, _source = resolve_copilot_token()
        entry = _jwt_cache.get(_token_fingerprint(raw_token)) if raw_token else None
    except Exception as exc:  # an unreadable secret store: the configured-host check still applies
        logger.debug("Copilot exchanged base URL lookup failed (%s)", type(exc).__name__)
        return True
    return not (entry and entry[2]) or is_copilot_host(entry[2])


# Saved ``model.api_mode`` values that keep the saved default Claude model on Chat Completions: earlier
# versions of this plugin's setup saved one (``chat_completions`` for Claude, ``codex_responses`` for
# GPT-5+), and a pick saved on an older core does too. Core honors the saved value for ``model.default``
# and rescues ``codex_responses`` to ``chat_completions`` only when the profile answers ``chat_completions``.
_LEGACY_SAVED_API_MODES = frozenset({"chat_completions", "codex_responses"})


def _bare(model_id: object) -> str:
    return str(model_id or "").strip().lower().rsplit("/", 1)[-1]  # ``anthropic/claude-...`` too


def _legacy_api_mode_pins(model_id: str) -> bool:
    """Whether a saved Copilot ``model.api_mode`` still decides *model_id*'s wire: core applies a saved
    value to the saved ``model.default`` only, so this is true when *model_id* is that default and the saved
    value is a legacy one. Claude then answers ``chat_completions`` for it, so core's ``codex_responses``
    rescue fires and ``/model`` / resume (which ask the profile) agree with startup (which honors the saved
    value). Other Claude models use /v1/messages. ``hermes model`` or ``hermes config unset model.api_mode``
    removes the value; a ``/model --global`` pick on a core with the fix no longer saves one."""
    try:
        model = _copilot_model_config()
        saved = str(model.get("api_mode") or "").strip().lower()
        return saved in _LEGACY_SAVED_API_MODES and _bare(model.get("default")) == _bare(model_id)
    except Exception as exc:  # an unreadable config keeps the new route
        logger.debug("Copilot saved api_mode lookup failed (%s)", type(exc).__name__)
        return False


def uses_messages_api(model_id: str) -> bool:
    """Claude models go to Copilot's Anthropic ``/v1/messages`` endpoint when core can drive it (Bearer auth
    for the Anthropic client and a saved ``api_mode`` scoped to the saved default: ``core_serves_messages_route``),
    the configured and exchanged base URLs are Copilot hosts (a proxy would get core's generic ``x-api-key``
    auth there), and no legacy saved ``api_mode`` pins this model: it honors ``cache_control`` TTLs and
    reports cache writes, which ``/chat/completions`` does not."""
    bare = _bare(model_id)
    return (bare.startswith("claude-") and not bare.endswith(_MESSAGES_UNSUPPORTED_SUFFIXES)
            and core_serves_messages_route() and is_copilot_host(_configured_base_url())
            and _exchanged_base_urls_are_copilot_hosts() and not _legacy_api_mode_pins(model_id))


def _api_mode_for_id(model_id: str) -> str:
    if requires_responses_api(model_id):
        return "codex_responses"
    if uses_messages_api(model_id):
        return "anthropic_messages"
    _warn_if_a_saved_messages_wire_pins_every_model()
    return "chat_completions"


_WARNED_SAVED_MESSAGES_WIRE: set[str] = set()


def _warn_if_a_saved_messages_wire_pins_every_model() -> None:
    """On a core without saved-api_mode scoping, a ``model.api_mode: anthropic_messages`` an earlier build of
    this plugin let ``/model --global`` save applies to every Copilot model, so GPT and Gemini requests go to
    /v1/messages and fail; no profile answer can override it. Say once per process how to remove it."""
    if core_scopes_saved_api_mode():
        return
    try:
        saved = str(_copilot_model_config().get("api_mode") or "").strip().lower()
    except Exception as exc:  # an unreadable config: nothing to warn about
        logger.debug("Copilot saved api_mode lookup failed (%s)", type(exc).__name__)
        return
    if saved == "anthropic_messages" and saved not in _WARNED_SAVED_MESSAGES_WIRE:
        _WARNED_SAVED_MESSAGES_WIRE.add(saved)
        logger.warning("config.yaml pins model.api_mode: anthropic_messages for GitHub Copilot; this Hermes "
                       "version applies it to every Copilot model, so non-Claude models fail. Run `hermes "
                       "config unset model.api_mode` (or `hermes model`).")


def model_api_mode(
    model_id: Optional[str], *, catalog: Optional[list[dict[str, Any]]] = None,
    api_key: Optional[str] = None) -> str:
    """API mode for a Copilot model from the id pattern (opencode's approach): GPT-5+ on Responses,
    Claude on Anthropic Messages (when core can drive that route), everything else on Chat Completions."""
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
