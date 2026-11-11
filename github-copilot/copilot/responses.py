"""Copilot / GitHub Models wire quirks on the Responses route, reasoning payloads and reasoning carriers.

Fills the generic ``ProviderProfile`` endpoint seams (providers/base.py, lane L3 block). Everything here
is called per request or per replayed message, so it is pure and answers from static data / caches.
"""

from __future__ import annotations

from typing import Any

from .helpers import base_url_host_matches

_COPILOT_HOST = "githubcopilot.com"  # api.githubcopilot.com plus the api.<plan>.githubcopilot.com variants


def _is_connection_bound_replay_error(status_code: int | None, message: str) -> bool:
    return status_code == 401 and "input item does not belong to this connection" in (message or "").lower()


def _classify_responses_error(error, *, status_code, error_code, message, body, model):
    # The classifier's combined message can also contain unrelated body fields. When a
    # structured body exists, inspect only its actual message, not a code or metadata.
    if isinstance(body, dict) and body:
        nested = body.get("error")
        detail = nested.get("message") if isinstance(nested, dict) else body.get("message")
    else:
        detail = getattr(error, "message", "")
    if isinstance(detail, str) and _is_connection_bound_replay_error(status_code, detail):
        return {"reason": "invalid_encrypted_content", "retryable": False,
                "should_rotate_credential": False, "should_fallback": False}
    return None


# Constructor kwargs for the profile's L3 fields. ``github_responses`` is persisted on encrypted reasoning
# items in session history: renaming it orphans every stored blob (one cold cache + dropped reasoning on resume).
RESPONSES_PROFILE_FIELDS: dict[str, Any] = {
    "classify_api_error": _classify_responses_error,
    # The backend binds replayed item ids to a connection; a stale id 401s even when short (#32716).
    "responses_replay_item_ids": False,
    "responses_prompt_cache_key": False,
    "responses_issuer_kind": "github_responses",
    "responses_trusts_completed_status": False,
    "responses_native_compaction": False,
    # Reasoning text only comes back when ``summary: auto`` is sent (#46527); ``include`` is never sent.
    "responses_profile_reasoning": True,
    "message_carrier_opaque": "reasoning_opaque",
    "message_carrier_text": "reasoning_text",
    # Copilot's Gemini models take tool-call ``extra_content`` signatures back like the native Gemini API.
    "replays_tool_call_signatures": True,
}


def _is_copilot_host(base_url: str) -> bool:
    return base_url_host_matches(str(base_url or ""), _COPILOT_HOST)


class CopilotResponsesMixin:
    """Endpoint ownership, catalog-clamped reasoning payloads and the ``/chat/completions`` carriers."""

    def is_stale_credential_error(self, *, status_code: int | None, message: str) -> bool:
        if _is_connection_bound_replay_error(status_code, message):
            return False
        return super().is_stale_credential_error(status_code=status_code, message=message)

    def reasoning_payload(self, model: str | None, reasoning_config: dict | None, *, base_url: str) -> dict | None:
        from .catalog import clamp_github_reasoning_effort, github_model_reasoning_efforts

        supported = github_model_reasoning_efforts(model)
        if not supported:
            return None
        cfg = reasoning_config if isinstance(reasoning_config, dict) else {}
        if cfg.get("enabled") is False:
            return None
        return {"effort": clamp_github_reasoning_effort(cfg.get("effort"), supported)}

    def replay_message_carriers(self, values: dict[str, Any], *, model: str | None, base_url: str) -> dict[str, Any]:
        # The editor clients replay reasoning_text only next to the opaque blob it belongs to.
        if not _is_copilot_host(base_url) or not values.get("reasoning_opaque"):
            return {}
        out = {"reasoning_opaque": values["reasoning_opaque"]}
        if values.get("reasoning_text"):
            out["reasoning_text"] = values["reasoning_text"]
        return out

    def replays_function_thought_signature(self, *, model: str | None, base_url: str) -> bool:
        # Copilot's Gemini 3 variant signs tool calls inside ``function`` and expects them back.
        return _is_copilot_host(base_url)
