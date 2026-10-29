"""Copilot catalog and model metadata: hosts, models.dev id, the live ``/models`` catalog (picker,
context windows, id normalization, reasoning efforts) and the curated fallback list.

Fills the generic ProviderProfile catalog seams (``providers/base.py`` L4 + I2-cat blocks).
"""

from __future__ import annotations

import copy
import logging
import time
from typing import Any, Optional

from .helpers import ASTRA_EFFORTS, base_url_host_matches, clamp_effort, get_json, is_astra_model

logger = logging.getLogger(__name__)

# Copilot business/enterprise accounts are served from ``api.<plan>.githubcopilot.com``; GitHub Models
# (models.github.ai) speaks the same wire (Responses quirks, reasoning payloads, x-initiator billing).
# Per-site differences (carriers, editor headers: githubcopilot.com only) live in the hooks, keyed on base_url.
HOSTNAMES = ("githubcopilot.com", "models.github.ai")
# The ~8K-capped GitHub Models free tier shares the Copilot catalog only (model metadata lookups).
METADATA_HOSTNAMES = ("models.inference.ai.azure.com",)
MODELS_DEV_ID = "github-copilot"

COPILOT_BASE_URL = "https://api.githubcopilot.com"
COPILOT_MODELS_URL = f"{COPILOT_BASE_URL}/models"
# Hosts (and subdomains) whose ``/models`` probe is answered from the Copilot catalog.
_CATALOG_HOSTS = HOSTNAMES + METADATA_HOSTNAMES
REASONING_EFFORTS_GPT5 = ["minimal", "low", "medium", "high"]
REASONING_EFFORTS_O_SERIES = ["low", "medium", "high"]

# Curated list (core's static catalog row): served when the live catalog is unreachable.
STATIC_MODELS = (
    "gpt-5.4", "gpt-5.4-mini", "gpt-5-mini", "gpt-5.3-codex", "gpt-5.2-codex", "gpt-4.1", "gpt-4o", "gpt-4o-mini",
    "claude-sonnet-4.6", "claude-sonnet-5", "claude-sonnet-4", "claude-sonnet-4.5", "claude-haiku-4.5",
    "gemini-3.1-pro-preview", "gemini-3-pro-preview", "gemini-3-flash-preview", "gemini-2.5-pro",
)

# OpenRouter-style ids -> Copilot ids. Dash-notation Claude ids are accepted too: Hermes' default
# Claude IDs use hyphens (Anthropic native) but Copilot's API only accepts dot-notation, so a
# copilot + hyphenated default would otherwise hit HTTP 400 "model_not_supported".
MODEL_ALIASES: dict[str, str] = dict((
    ("openai/gpt-5", "gpt-5-mini"), ("openai/gpt-5-chat", "gpt-5-mini"), ("openai/gpt-5-mini", "gpt-5-mini"),
    ("openai/gpt-5-nano", "gpt-5-mini"), ("openai/gpt-4.1", "gpt-4.1"), ("openai/gpt-4.1-mini", "gpt-4.1"),
    ("openai/gpt-4.1-nano", "gpt-4.1"), ("openai/gpt-4o", "gpt-4o"), ("openai/gpt-4o-mini", "gpt-4o-mini"),
    ("openai/o1", "gpt-5.2"), ("openai/o1-mini", "gpt-5-mini"), ("openai/o1-preview", "gpt-5.2"),
    ("openai/o3", "gpt-5.3-codex"), ("openai/o3-mini", "gpt-5-mini"), ("openai/o4-mini", "gpt-5-mini"),
    ("anthropic/claude-opus-4.6", "claude-opus-4.6"), ("anthropic/claude-sonnet-5", "claude-sonnet-5"),
    ("anthropic/claude-sonnet-4.6", "claude-sonnet-4.6"), ("anthropic/claude-sonnet-4", "claude-sonnet-4"),
    ("anthropic/claude-sonnet-4.5", "claude-sonnet-4.5"), ("anthropic/claude-haiku-4.5", "claude-haiku-4.5"),
    ("claude-sonnet-5", "claude-sonnet-5"), ("claude-opus-4-6", "claude-opus-4.6"),
    ("claude-sonnet-4-6", "claude-sonnet-4.6"), ("claude-sonnet-4-0", "claude-sonnet-4"),
    ("claude-sonnet-4-5", "claude-sonnet-4.5"), ("claude-haiku-4-5", "claude-haiku-4.5"),
    ("anthropic/claude-opus-4-6", "claude-opus-4.6"), ("anthropic/claude-sonnet-4-6", "claude-sonnet-4.6"),
    ("anthropic/claude-sonnet-4-0", "claude-sonnet-4"), ("anthropic/claude-sonnet-4-5", "claude-sonnet-4.5"),
    ("anthropic/claude-haiku-4-5", "claude-haiku-4.5"),
))


# ─── catalog credential (best effort) ───

def _first_exchangeable_copilot_token(raw_tokens) -> str:
    """Exchange stored GitHub tokens in order; the first that validates AND exchanges wins (every
    entry is tried so a later valid token survives an earlier malformed one)."""
    from .credentials import exchange_copilot_token, validate_copilot_token

    for raw in raw_tokens:
        raw = str(raw or "").strip()
        if not raw or not validate_copilot_token(raw)[0]:
            continue
        try:
            api_token = exchange_copilot_token(raw)[0]  # (api_token, expires_at, base_url)
        except Exception:
            continue
        if api_token:
            return api_token
    return ""


def _resolve_copilot_catalog_api_key() -> str:
    """Best-effort Copilot API token for the catalog: the exchanged env token (COPILOT_GITHUB_TOKEN,
    GH_TOKEN, GITHUB_TOKEN), then ``auth.json`` ``credential_pool.copilot[]`` (a ``hermes auth add
    copilot`` login). Without the pool, keyless users see the picker fall back to the stale curated
    list on a silent 401."""
    def _env_token() -> str:
        from .credentials import get_copilot_api_token, resolve_copilot_token

        raw, _source = resolve_copilot_token()
        return get_copilot_api_token(raw)[0] if raw else ""

    def _pool_token() -> str:
        from agent.credential_pool import read_credential_pool

        return _first_exchangeable_copilot_token(
            entry.get("access_token") for entry in read_credential_pool("copilot") if isinstance(entry, dict))

    sources = (_env_token, _pool_token)
    for source in sources:
        try:
            token = source()
        except Exception:
            continue
        if token:
            return token
    return ""


# ─── live /models catalog ───

def copilot_default_headers(*, is_agent_turn: bool = True, api_key: str = "") -> dict[str, str]:
    """Standard headers for Copilot API requests (``api_key`` selects the caller attribution)."""
    from .credentials import copilot_request_headers

    return copilot_request_headers(is_agent_turn=is_agent_turn, token=api_key)


_COPILOT_CHAT_ENDPOINTS = {"/chat/completions", "/responses", "/v1/messages"}


def _payload_items(payload: Any) -> list[dict[str, Any]]:
    data = payload.get("data", []) if isinstance(payload, dict) else payload
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _copilot_catalog_item_is_text_model(
    item: dict[str, Any], *, ignore_picker_flag: bool = False) -> bool:
    if not str(item.get("id") or "").strip():
        return False
    if not ignore_picker_flag and item.get("model_picker_enabled") is False:
        return False
    capabilities = item.get("capabilities")
    if isinstance(capabilities, dict):
        model_type = str(capabilities.get("type") or "").strip().lower()
        if model_type and model_type != "chat":
            return False
    supported_endpoints = item.get("supported_endpoints")
    if isinstance(supported_endpoints, list):
        endpoints = {e for endpoint in supported_endpoints if (e := str(endpoint).strip())}
        if endpoints and not endpoints & _COPILOT_CHAT_ENDPOINTS:
            return False
    return True


def _copilot_text_models(items: list[dict[str, Any]], *, ignore_picker_flag: bool = False) -> list[dict[str, Any]]:
    """Chat-capable catalog rows, deduped by id, in catalog order."""
    models: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in items:
        model_id = str(item.get("id") or "").strip()
        if model_id in seen_ids:
            continue
        if not _copilot_catalog_item_is_text_model(item, ignore_picker_flag=ignore_picker_flag):
            continue
        seen_ids.add(model_id)
        models.append(item)
    return models


# Short-TTL cache of the filtered GitHub Copilot /models catalog (picker + context/normalize helpers
# share it). Keyed by the api_key of the successful fetch so a credential swap never serves the
# previous account's catalog; monotonic clock; lock-free (a race at worst duplicates one fetch).
_github_model_catalog_cache: Optional[list[dict[str, Any]]] = None
_github_model_catalog_cache_key: Optional[str] = None
_github_model_catalog_cache_time: float = 0.0
_GITHUB_MODEL_CATALOG_CACHE_TTL = 300  # 5 minutes


def fetch_github_model_catalog(
    api_key: Optional[str] = None, timeout: float = 5.0) -> Optional[list[dict[str, Any]]]:
    """Fetch the live GitHub Copilot model catalog for this account."""
    global _github_model_catalog_cache, _github_model_catalog_cache_key
    global _github_model_catalog_cache_time
    if (
        _github_model_catalog_cache is not None
        and _github_model_catalog_cache_key == api_key
        and (time.monotonic() - _github_model_catalog_cache_time) < _GITHUB_MODEL_CATALOG_CACHE_TTL
    ):
        return copy.deepcopy(_github_model_catalog_cache)  # deep: callers must not mutate cached dicts

    attempts: list[dict[str, str]] = []
    if api_key:
        attempts.append({**copilot_default_headers(api_key=api_key), "Authorization": f"Bearer {api_key}"})
    attempts.append(copilot_default_headers())

    for headers in attempts:
        try:
            items = _payload_items(get_json(COPILOT_MODELS_URL, timeout=timeout, headers=headers))
        except Exception:
            continue
        models = _copilot_text_models(items)
        if not models and items:
            # GitHub has been observed returning ``model_picker_enabled: false`` for EVERY model on
            # some accounts, which would strand the picker on the stale curated fallback. The flag
            # is a display hint, not an availability contract — retry without it (chat/endpoint
            # checks still apply).
            models = _copilot_text_models(items, ignore_picker_flag=True)
        if models:
            _github_model_catalog_cache = copy.deepcopy(models)
            _github_model_catalog_cache_key = api_key
            _github_model_catalog_cache_time = time.monotonic()
            return models
    return None


def _fetch_github_models(api_key: Optional[str] = None, timeout: float = 5.0) -> Optional[list[str]]:
    catalog = fetch_github_model_catalog(api_key=api_key, timeout=timeout)
    return [item.get("id", "") for item in catalog if item.get("id")] if catalog else None


def copilot_picker_catalog(force_refresh: bool = False) -> Optional[list[str]]:
    """The account's live catalog ids, else None: core then serves ``STATIC_MODELS`` (the profile's
    ``static_models``) marked as a placeholder, never cached as the account's catalog."""
    from .activation import github_calls_allowed

    try:
        # Provider listings (``hermes status``, pickers, Desktop) warm every listed provider's
        # catalog; a home that never chose Copilot keeps the curated list and GitHub is not called.
        live = _fetch_github_models(_resolve_copilot_catalog_api_key()) if github_calls_allowed() else None
        if live:
            return live
    except Exception:  # the picker degrades to the curated list on any catalog failure
        logger.debug("copilot live catalog fetch failed", exc_info=True)
    return None


def _is_github_models_base_url(base_url: Optional[str]) -> bool:
    # Host match (never a URL prefix): the plan hosts api.<plan>.githubcopilot.com share the account's
    # catalog, and ``https://api.githubcopilot.com.example`` is not GitHub.
    return any(base_url_host_matches(str(base_url or "").strip(), host) for host in _CATALOG_HOSTS)


# ─── context windows ───

# Module-level cache: {model_id: max_prompt_tokens}
_copilot_context_cache: dict[str, int] = {}
_copilot_context_cache_time: float = 0.0
_copilot_context_cache_key: Optional[str] = None  # fingerprint of the api_key the entry was fetched with
_COPILOT_CONTEXT_CACHE_TTL = 3600  # 1 hour


def get_copilot_model_context(model_id: str, api_key: Optional[str] = None) -> Optional[int]:
    """``max_prompt_tokens`` for a Copilot model from the live /models API (cached in-process 1h; a
    miss on a fresh cache does not re-fetch), or None."""
    global _copilot_context_cache, _copilot_context_cache_time, _copilot_context_cache_key

    # Keyed on the credential like fetch_github_model_catalog: the catalog (and its limits) is
    # per-account, so another profile's token must not be served this entry.
    from .credentials import _token_fingerprint

    key_fp = _token_fingerprint(api_key) if api_key else None
    if (_copilot_context_cache and _copilot_context_cache_key == key_fp
            and (time.time() - _copilot_context_cache_time < _COPILOT_CONTEXT_CACHE_TTL)):
        return _copilot_context_cache.get(model_id)

    catalog = fetch_github_model_catalog(api_key=api_key)
    if not catalog:
        return None
    cache: dict[str, int] = {}
    for item in catalog:
        mid = str(item.get("id") or "").strip()
        max_prompt = ((item.get("capabilities") or {}).get("limits") or {}).get("max_prompt_tokens")
        if mid and isinstance(max_prompt, int) and max_prompt > 0:
            cache[mid] = max_prompt
    _copilot_context_cache = cache
    _copilot_context_cache_time = time.time()
    _copilot_context_cache_key = key_fp
    return cache.get(model_id)


# ─── id normalization and reasoning efforts ───

def _copilot_catalog_ids(
    catalog: Optional[list[dict[str, Any]]] = None, api_key: Optional[str] = None) -> set[str]:
    if catalog is None and api_key:
        catalog = fetch_github_model_catalog(api_key=api_key)
    return {mid for item in (catalog or []) if (mid := str(item.get("id") or "").strip())}


def normalize_copilot_model_id(
    model_id: Optional[str], *, catalog: Optional[list[dict[str, Any]]] = None,
    api_key: Optional[str] = None) -> str:
    raw = str(model_id or "").strip()
    if not raw:
        return ""

    catalog_ids = _copilot_catalog_ids(catalog=catalog, api_key=api_key)
    alias = MODEL_ALIASES.get(raw)
    if alias:
        return alias

    candidates = [raw]
    if "/" in raw:
        candidates.append(raw.split("/", 1)[1].strip())
    if raw.endswith(("-mini", "-nano", "-chat")):
        candidates.append(raw[:-5])

    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        if candidate in MODEL_ALIASES:
            return MODEL_ALIASES[candidate]
        if candidate in catalog_ids:
            return candidate

    if "/" in raw:
        stripped = raw.split("/", 1)[1].strip()
        # Enterprise BYOK custom models expose ``owner/sub/model`` ids (two
        # slashes). A strip guess that still contains "/" cannot be a Copilot
        # id, so pass the input through untouched instead of corrupting it.
        if stripped and "/" not in stripped:
            return stripped
    return raw


def clamp_github_reasoning_effort(effort: Any, supported: list[str]) -> str:
    """Copilot/GitHub Models effort for a non-empty *supported* list: the level itself when listed,
    else the nearest WEAKER listed level; bespoke names the ladder can't place fall to ``medium``
    (or the first listed level)."""
    effort = str(effort or "medium").strip().lower()
    if effort not in supported:
        effort = clamp_effort(effort, supported)
        if effort not in supported:
            effort = "medium" if "medium" in supported else supported[0]
    return effort


def _github_reasoning_efforts_for_model_id(model_id: str) -> list[str]:
    raw = (model_id or "").strip().lower()
    if raw.startswith(("openai/o1", "openai/o3", "openai/o4", "o1", "o3", "o4")):
        return list(REASONING_EFFORTS_O_SERIES)
    normalized = normalize_copilot_model_id(model_id).lower()
    if is_astra_model(normalized):
        return list(ASTRA_EFFORTS)
    if normalized.startswith("gpt-5"):
        return list(REASONING_EFFORTS_GPT5)
    return []


def github_model_reasoning_efforts(
    model_id: Optional[str], *, catalog: Optional[list[dict[str, Any]]] = None,
    api_key: Optional[str] = None) -> list[str]:
    """Return supported reasoning-effort levels for a Copilot-visible model."""
    normalized = normalize_copilot_model_id(model_id, catalog=catalog, api_key=api_key)
    if not normalized:
        return []

    if catalog is None and api_key:
        catalog = fetch_github_model_catalog(api_key=api_key)
    catalog_entry = next((item for item in catalog if item.get("id") == normalized), None) if catalog else None
    if catalog_entry is not None:
        capabilities = catalog_entry.get("capabilities")
        if isinstance(capabilities, dict):
            # Structured catalog: the advertised list is authoritative (empty when absent).
            supports = capabilities.get("supports")
            efforts = supports.get("reasoning_effort") if isinstance(supports, dict) else None
            if not isinstance(efforts, list):
                return []
            return list(dict.fromkeys(e for effort in efforts if (e := str(effort).strip().lower())))
        # Legacy list-shaped capabilities: only a "reasoning" tag unlocks the pattern defaults.
        if "reasoning" not in {str(c).strip().lower() for c in catalog_entry.get("capabilities", [])}:
            return []
    return _github_reasoning_efforts_for_model_id(str(model_id or normalized))


# ProviderProfile kwargs filling the I2-cat catalog seams.
CATALOG_FIELDS = dict(picker_catalog=copilot_picker_catalog, static_models=STATIC_MODELS)


class CopilotCatalogMixin:
    """Catalog/metadata hooks for the Copilot profiles."""

    def live_context_length(self, model: str, *, api_key: str = "", base_url: str = "") -> int | None:
        return get_copilot_model_context(model, api_key=api_key)

    def normalize_model_id(self, model: str, *, api_key: str = "") -> str | None:
        return normalize_copilot_model_id(model, api_key=api_key or None)

    def probe_models(
        self, *, base_url: str, api_key: str | None = None, timeout: float = 5.0,
    ) -> tuple[list[str] | None, str, str] | None:
        if not _is_github_models_base_url(base_url):
            return None
        return _fetch_github_models(api_key=api_key, timeout=timeout), COPILOT_MODELS_URL, COPILOT_BASE_URL
