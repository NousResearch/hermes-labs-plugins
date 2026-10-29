"""GitHub Copilot wire quirks: per-model api_mode, editor/initiator/vision headers, max-tokens param.

Fills the generic ProviderProfile wire seams (``providers/base.py`` L1 block). Every hook here is
pure and answers from the arguments alone, except ``api_mode_for_model`` with an ``api_key``, which
may read the account's cached live catalog to normalize the model id first.
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


def model_api_mode(
    model_id: Optional[str], *, catalog: Optional[list[dict[str, Any]]] = None,
    api_key: Optional[str] = None) -> str:
    """API mode for a Copilot model from the id pattern (opencode's approach). Copilot's Claude models
    go through its OpenAI-compatible chat endpoint, not the native Anthropic adapter: the catalog may
    advertise /v1/messages but the Copilot token/header scheme lives in the OpenAI client path."""
    from .catalog import fetch_github_model_catalog, normalize_copilot_model_id

    if catalog is None and api_key:  # fetch once so normalize + endpoint check share it
        catalog = fetch_github_model_catalog(api_key=api_key)
    normalized = normalize_copilot_model_id(model_id, catalog=catalog, api_key=api_key)
    if normalized and requires_responses_api(normalized):
        return "codex_responses"
    return "chat_completions"


def editor_headers(*, is_vision: bool = False, token: str = "") -> dict[str, str]:
    from .credentials import copilot_request_headers

    return copilot_request_headers(is_agent_turn=True, is_vision=is_vision, token=token)


class CopilotWireMixin:
    """ProviderProfile wire hooks for the Copilot API."""

    def api_mode_for_model(self, model: str, *, api_key: str | None = None) -> str | None:
        if api_key is None:
            return "codex_responses" if requires_responses_api(model) else "chat_completions"
        return model_api_mode(model, api_key=api_key)

    def client_headers(
        self, *, base_url: str, api_key: str = "", has_images: bool = False,
    ) -> dict[str, str] | None:
        return editor_headers(is_vision=has_images, token=api_key) if is_copilot_host(base_url) else None

    def request_headers(self, *, base_url: str, model: str = "", user_initiated: bool = False) -> dict[str, str]:
        # First call of a user turn says ``user``; follow-ups keep the client default ``x-initiator: agent``.
        if user_initiated and any(base_url_host_matches(base_url, h) for h in _INITIATOR_HOSTS):
            return {"x-initiator": "user"}
        return {}

    def max_tokens_param(self, *, base_url: str, model: str = "") -> str | None:
        host = base_url_hostname(str(base_url or "")) or ""
        return "max_completion_tokens" if host.endswith("." + _COPILOT_HOST) else None
