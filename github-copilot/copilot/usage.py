"""GitHub Copilot account quota fetcher for ``/usage`` (``ProviderProfile.fetch_account_usage``).

Queries ``GET https://api.github.com/copilot_internal/user`` with the raw GitHub token
(``Authorization: token <gho_*|ghu_*|github_pat_*>`` — never the exchanged Copilot API token) and
maps ``quota_snapshots`` (plus legacy ``monthly_quotas`` / ``limited_user_quotas`` payloads) to an
:class:`agent.account_usage.AccountUsageSnapshot`.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any, Optional

from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow
from agent.credential_pool import STATUS_DEAD, read_credential_pool
from agent.secret_scope import get_secret

from .activation import github_calls_allowed
from .credentials import (
    COPILOT_ENV_VARS,
    _EDITOR_VERSION,
    _EXCHANGE_USER_AGENT,
    _GITHUB_API_VERSION,
    _jwt_cache,
    _load_jwt_from_disk,
    _token_fingerprint,
    resolve_copilot_token,
    validate_copilot_token,
)
from .helpers import base_url_host_matches, get_json

COPILOT_USER_URL = "https://api.github.com/copilot_internal/user"
_USAGE_TIMEOUT_SECONDS = 8.0
_GITHUB_MODELS_HOSTS = ("models.github.ai", "models.inference.ai.azure.com")
_QUOTA_ORDER = ("premium_interactions", "chat", "completions")
_QUOTA_LABELS = {
    "premium_interactions": "Premium requests",
    "chat": "Chat",
    "completions": "Completions",
}


def _is_finite_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _fmt_num(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _title_slug(value: Optional[str]) -> Optional[str]:
    cleaned = str(value or "").strip()
    return cleaned.replace("_", " ").replace("-", " ").title() if cleaned else None


def _parse_dt(value: Any) -> Optional[datetime]:
    if value in (None, "") or isinstance(value, bool):
        return None
    if _is_finite_num(value):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OSError, OverflowError, ValueError):
            return None
    if not isinstance(value, str) or not (text := value.strip()):
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _is_github_models_url(base_url: Optional[str]) -> bool:
    raw = str(base_url or "").strip()
    return bool(raw) and any(base_url_host_matches(raw, host) for host in _GITHUB_MODELS_HOSTS)


def _pool_raw_tokens_and_env_matches(explicit_api_key: str = "") -> tuple[list[str], str]:
    """Non-dead raw GitHub tokens from ``credential_pool.copilot`` plus any ``env:<VAR>`` match for
    an exchanged *explicit_api_key*."""
    raw_tokens: list[str] = []
    matched_env_raw = ""
    for row in read_credential_pool("copilot") or ():
        if not isinstance(row, dict) or row.get("last_status") == STATUS_DEAD:
            continue
        tok = str(row.get("access_token") or "").strip()
        source = str(row.get("source") or "").strip()
        if source.startswith("env:"):
            env_var = source.removeprefix("env:")
            if env_var in COPILOT_ENV_VARS:
                env_raw = (get_secret(env_var) or "").strip()
                if env_raw and validate_copilot_token(env_raw)[0]:
                    raw_tokens.append(env_raw)
                    if explicit_api_key and tok == explicit_api_key and not matched_env_raw:
                        matched_env_raw = env_raw
            continue
        if tok and validate_copilot_token(tok)[0]:
            raw_tokens.append(tok)
    return raw_tokens, matched_env_raw


def _exchanged_token_matches(raw_token: str, api_token: str) -> bool:
    fp = _token_fingerprint(raw_token)
    for cached in (_jwt_cache.get(fp), _load_jwt_from_disk(fp)):
        if cached and cached[0] == api_token:
            return True
    return False


def resolve_copilot_usage_token(api_key: Optional[str] = None) -> str:
    """Resolve the raw GitHub token to query ``/copilot_internal/user`` with, or ``""``.

    When *api_key* is supplied (a live session's exchanged Copilot token or a per-credential pool
    check), only the raw token backing THAT credential is returned so multi-account pools never
    attribute one account's quota to another.
    """
    explicit = str(api_key or "").strip()
    if explicit:
        if validate_copilot_token(explicit)[0]:
            return explicit
        pool_raws, matched_env_raw = _pool_raw_tokens_and_env_matches(explicit)
        if matched_env_raw:
            return matched_env_raw
        env_raws = [
            val for var in COPILOT_ENV_VARS
            if (val := (get_secret(var) or "").strip()) and validate_copilot_token(val)[0]
        ]
        candidates = list(dict.fromkeys([*env_raws, *pool_raws]))
        for raw in candidates:
            if _exchanged_token_matches(raw, explicit):
                return raw
        return candidates[0] if len(candidates) == 1 else ""

    if not github_calls_allowed():
        return ""
    raw, _source = resolve_copilot_token()
    if raw:
        return raw
    pool_raws, _ = _pool_raw_tokens_and_env_matches()
    return pool_raws[0] if pool_raws else ""


def _resolve_plan_label(payload: dict[str, Any]) -> Optional[str]:
    plan = str(payload.get("copilot_plan") or "").strip()
    sku = str(payload.get("access_type_sku") or "").strip()
    if sku.lower() == "free_limited_copilot" or plan.lower() == "free":
        return "Free"
    return _title_slug(plan) or _title_slug(sku)


def _resolve_reset_at(payload: dict[str, Any], entry: Optional[dict[str, Any]] = None) -> Optional[datetime]:
    if isinstance(entry, dict):
        for key in ("reset_date_utc", "reset_date", "resets_at"):
            if (parsed := _parse_dt(entry.get(key))) is not None:
                return parsed
    for key in ("quota_reset_date_utc", "quota_reset_date", "limited_user_reset_date"):
        if (parsed := _parse_dt(payload.get(key))) is not None:
            return parsed
    return None


def _window_from_quota_entry(
    key: str, entry: dict[str, Any], default_reset: Optional[datetime],
) -> tuple[Optional[AccountUsageWindow], Optional[str], Optional[str]]:
    """Build ``(window, count_summary, overage_line)`` for one capped ``quota_snapshots`` bucket."""
    entitlement = entry.get("entitlement")
    remaining = entry.get("remaining")
    pct_remaining = entry.get("percent_remaining")
    credits_used = entry.get("credits_used")

    if _is_finite_num(entitlement) and float(entitlement) <= 0:
        return None, None, None

    used_pct: Optional[float] = None
    if _is_finite_num(pct_remaining):
        used_pct = max(0.0, min(100.0, 100.0 - float(pct_remaining)))
    elif _is_finite_num(entitlement) and float(entitlement) > 0:
        ent_f = float(entitlement)
        if _is_finite_num(remaining):
            used_pct = max(0.0, min(100.0, (ent_f - float(remaining)) / ent_f * 100.0))
        elif _is_finite_num(credits_used):
            used_pct = max(0.0, min(100.0, float(credits_used) / ent_f * 100.0))
    if used_pct is None:
        return None, None, None

    label = _QUOTA_LABELS.get(key) or _title_slug(key) or key
    count_summary: Optional[str] = None
    detail: Optional[str] = None
    if _is_finite_num(entitlement) and float(entitlement) > 0:
        ent_f = float(entitlement)
        rem_f: Optional[float] = None
        if _is_finite_num(remaining):
            rem_f = max(0.0, float(remaining))
        elif _is_finite_num(credits_used):
            rem_f = max(0.0, ent_f - float(credits_used))
        if rem_f is not None:
            ratio = f"{_fmt_num(rem_f)}/{_fmt_num(ent_f)}"
            detail = f"{ratio} remaining"
            count_summary = f"{label} {ratio}"

    overage_line: Optional[str] = None
    if entry.get("overage_permitted") is True:
        overage_count = entry.get("overage_count")
        if _is_finite_num(overage_count) and float(overage_count) > 0:
            overage_line = f"Overage: enabled ({_fmt_num(float(overage_count))} used)"
        else:
            overage_line = "Overage: enabled"

    reset_at = _resolve_reset_at({}, entry) or default_reset
    scope = "account" if key == "chat" else "model"
    window = AccountUsageWindow(label=label, used_percent=used_pct, reset_at=reset_at, detail=detail, scope=scope)
    return window, count_summary, overage_line


def build_copilot_usage_snapshot(payload: Any) -> Optional[AccountUsageSnapshot]:
    """Normalize a ``/copilot_internal/user`` JSON object into an ``AccountUsageSnapshot``."""
    if not isinstance(payload, dict):
        return None

    default_reset = _resolve_reset_at(payload)
    windows: list[AccountUsageWindow] = []
    reset_hidden_counts: list[str] = []
    unlimited_labels: list[str] = []
    overage_lines: list[str] = []

    snapshots = payload.get("quota_snapshots")
    if isinstance(snapshots, dict) and snapshots:
        ordered_keys = [*_QUOTA_ORDER, *(k for k in snapshots if k not in _QUOTA_ORDER)]
        for key in ordered_keys:
            entry = snapshots.get(key)
            if not isinstance(entry, dict) or entry.get("has_quota") is False:
                continue
            label = _QUOTA_LABELS.get(key) or _title_slug(key) or key
            if entry.get("unlimited") is True:
                unlimited_labels.append(label)
                continue
            window, count_summary, overage_line = _window_from_quota_entry(key, entry, default_reset)
            if window is None:
                continue
            windows.append(window)
            if count_summary and window.reset_at is not None:
                reset_hidden_counts.append(count_summary)
            if overage_line and overage_line not in overage_lines:
                overage_lines.append(overage_line)
    else:
        monthly = payload.get("monthly_quotas")
        limited = payload.get("limited_user_quotas")
        if isinstance(monthly, dict) and isinstance(limited, dict):
            ordered_keys = [*_QUOTA_ORDER, *(k for k in monthly if k not in _QUOTA_ORDER)]
            for key in ordered_keys:
                ent, rem = monthly.get(key), limited.get(key)
                if not (_is_finite_num(ent) and float(ent) > 0 and _is_finite_num(rem)):
                    continue
                synthetic = {"entitlement": ent, "remaining": rem}
                window, count_summary, _ = _window_from_quota_entry(key, synthetic, default_reset)
                if window is not None:
                    windows.append(window)
                    if count_summary and window.reset_at is not None:
                        reset_hidden_counts.append(count_summary)

    details: list[str] = []
    if reset_hidden_counts:
        details.append("Quota remaining: " + " • ".join(reset_hidden_counts))
    if unlimited_labels:
        details.append("Unlimited: " + ", ".join(unlimited_labels))
    details.extend(overage_lines)

    if not windows and not details:
        return None

    return AccountUsageSnapshot(
        provider="copilot",
        source="copilot_internal_user",
        fetched_at=datetime.now(UTC),
        plan=_resolve_plan_label(payload),
        windows=tuple(windows),
        details=tuple(details),
        raw=payload,
    )


def fetch_copilot_account_usage(
    *, base_url: Optional[str] = None, api_key: Optional[str] = None, timeout: float = _USAGE_TIMEOUT_SECONDS,
) -> Optional[AccountUsageSnapshot]:
    """Fetch GitHub Copilot subscription quota for ``/usage``; None when inapplicable or unconfigured."""
    if _is_github_models_url(base_url):
        return None
    raw_token = resolve_copilot_usage_token(api_key)
    if not raw_token:
        return None
    headers = {
        "Authorization": f"token {raw_token}",
        "Accept": "application/json",
        "User-Agent": _EXCHANGE_USER_AGENT,
        "Editor-Version": _EDITOR_VERSION,
        "X-GitHub-Api-Version": _GITHUB_API_VERSION,
    }
    payload = get_json(COPILOT_USER_URL, timeout=timeout, headers=headers)
    return build_copilot_usage_snapshot(payload)
