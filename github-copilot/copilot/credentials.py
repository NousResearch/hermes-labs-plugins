"""GitHub Copilot credentials: token resolution (COPILOT_GITHUB_TOKEN, GH_TOKEN, GITHUB_TOKEN, the order
the Copilot CLI uses, read through Hermes' secret scope), device-code login, the GitHub→Copilot token
exchange, and :class:`CopilotCredentialsMixin`, which fills core's generic ``ProviderProfile`` credential
seams.

Catalog rule 11 (credentials only through Hermes): this plugin never runs ``gh auth token`` and never
reads the gh CLI's or the Copilot CLI's token files. A GitHub token reaches it only through Hermes:
the env vars above (``.env`` or the process environment) or a ``hermes auth add copilot`` pool row."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

from agent.secret_scope import UnscopedSecretError, get_secret
from utils import atomic_json_write

logger = logging.getLogger(__name__)

# VS Code's GitHub App client ID: mints ghu_* tokens exchangeable for Copilot API JWTs (needed for
# internal-only models / enterprise endpoints). The opencode App ID mints gho_* tokens that 404.
COPILOT_OAUTH_CLIENT_ID = "Iv1.b507a08c87ecfe98"
_CLASSIC_PAT_PREFIX = "ghp_"  # rejected by the Copilot API
# Token families the Copilot API exchanges. Anything else (a random string in GITHUB_TOKEN) used
# to pass validation and then fail downstream with an opaque auth error (#12650).
_SUPPORTED_PREFIXES = ("gho_", "github_pat_", "ghu_")
COPILOT_ENV_VARS = ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")
_DEVICE_CODE_POLL_INTERVAL = 5  # seconds
_DEVICE_CODE_POLL_SAFETY_MARGIN = 3  # seconds


def validate_copilot_token(token: str) -> tuple[bool, str]:
    """Validate that a token is usable with the Copilot API."""
    token = token.strip()
    if not token:
        return False, "Empty token"
    if token.startswith(_CLASSIC_PAT_PREFIX):
        return False, (
            "Classic Personal Access Tokens (ghp_*) are not supported by the "
            "Copilot API. Use one of:\n"
            "  → `hermes auth add copilot` to sign in with GitHub (device code)\n"
            "  → A fine-grained PAT (github_pat_*) with Copilot Requests permission")
    if not token.startswith(_SUPPORTED_PREFIXES):
        return False, (
            "Unsupported GitHub token format for the Copilot API. "
            f"Supported token prefixes: {', '.join(_SUPPORTED_PREFIXES)}.")
    return True, "OK"


def resolve_copilot_token() -> tuple[str, str]:
    """GitHub token for Copilot API use from the first env var holding a supported one → (token, source
    env var name); ("", "") when none does."""
    for env_var in COPILOT_ENV_VARS:
        val = (get_secret(env_var) or "").strip()
        if not val:
            continue
        valid, msg = validate_copilot_token(val)
        if valid:
            return val, env_var
        logger.warning("Token from %s is not supported: %s", env_var, msg)
    return "", ""


_DEVICE_CODE_TERMINAL_ERRORS = {"expired_token": "  ✗ Device code expired. Please try again.",
                                "access_denied": "  ✗ Authorization was denied."}


def _post_form(url: str, fields: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        url, data=urllib.parse.urlencode(fields).encode(),
        headers={"Accept": "application/json", "User-Agent": "HermesAgent/1.0",
                 "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def copilot_device_code_login(
    *, host: str = "github.com", timeout_seconds: float = 300) -> Optional[str]:
    """Run the GitHub OAuth device code flow for Copilot."""
    domain = host.rstrip("/")
    try:
        device_data = _post_form(f"https://{domain}/login/device/code",
                                 {"client_id": COPILOT_OAUTH_CLIENT_ID, "scope": "read:user"}, 15)
    except Exception as exc:
        logger.error("Failed to initiate device authorization: %s", exc)
        print(f"  ✗ Failed to start device authorization: {exc}")
        return None

    verification_uri = device_data.get("verification_uri", "https://github.com/login/device")
    user_code = device_data.get("user_code", "")
    device_code = device_data.get("device_code", "")
    interval = max(device_data.get("interval", _DEVICE_CODE_POLL_INTERVAL), 1)
    if not device_code or not user_code:
        print("  ✗ GitHub did not return a device code.")
        return None
    print(f"\n  Open this URL in your browser: {verification_uri}\n"
          f"  Enter this code: {user_code}\n")
    print("  Waiting for authorization...", end="", flush=True)
    poll_fields = {"client_id": COPILOT_OAUTH_CLIENT_ID, "device_code": device_code,
                   "grant_type": "urn:ietf:params:oauth:grant-type:device_code"}
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        time.sleep(interval + _DEVICE_CODE_POLL_SAFETY_MARGIN)
        try:
            result = _post_form(f"https://{domain}/login/oauth/access_token", poll_fields, 10)
        except Exception:
            print(".", end="", flush=True)
            continue
        if result.get("access_token"):
            print(" ✓")
            return result["access_token"]
        error = result.get("error", "")
        if error == "slow_down":
            # RFC 8628: add 5 seconds to polling interval (or honor a server-supplied one)
            server_interval = result.get("interval")
            is_num = isinstance(server_interval, (int, float)) and server_interval > 0
            interval = int(server_interval) if is_num else interval + 5
        if error in ("authorization_pending", "slow_down"):
            print(".", end="", flush=True)
            continue
        if error:
            print("\n" + _DEVICE_CODE_TERMINAL_ERRORS.get(error,
                                                       f"  ✗ Authorization failed: {error}"))
            return None
    print("\n  ✗ Timed out waiting for authorization.")
    return None


# In-process cache: raw_token_fingerprint -> (api_token, expires_at_epoch, base_url).
_jwt_cache: dict[str, tuple[str, float, Optional[str]]] = {}
_JWT_REFRESH_MARGIN_SECONDS = 120  # refresh 2 min before expiry
# Exchange endpoint and headers (matching VS Code / Copilot CLI)
_TOKEN_EXCHANGE_URL = "https://api.github.com/copilot_internal/v2/token"
_EDITOR_VERSION = "vscode/1.104.1"
_EXCHANGE_USER_AGENT = "GitHubCopilotChat/0.26.7"

# Transient-failure hardening: gateway startup races network readiness, and a single-shot
# exchange failing there silently degrades to the RAW GitHub token, whose integrator allowlist
# omits enterprise-only models → HTTP 400 every turn until restart. Retry, and persist the last
# good JWT so a restart during a blip reuses the still-valid ~30-min token.
_EXCHANGE_MAX_ATTEMPTS = 3
_EXCHANGE_BACKOFF_BASE_SECONDS = 1.5  # sleeps ~1.5s, ~3.0s between attempts
_JWT_DISK_FILENAME = ".copilot_jwt.json"
_JWT_DISK_MAX_BYTES = 1_048_576  # 1 MiB cap on the persisted JWT store read
# Negative cache: fingerprint -> epoch until which attempts raise immediately (success clears
# it). Without it a permanently-rejected token burned ~4.5s of retry backoff on EVERY
# provider-discovery pass (/model picker, delegation spawns, dashboard).
_exchange_failure_cache: dict[str, float] = {}
# Single-flight per fingerprint: concurrent callers (dashboard polls every few seconds) wait on
# the ONE in-flight exchange instead of each spawning a hung resolver thread during a DNS outage.
_exchange_locks: dict[str, threading.Lock] = {}
_exchange_locks_guard = threading.Lock()


def _exchange_lock_for(fp: str) -> threading.Lock:
    with _exchange_locks_guard:
        lock = _exchange_locks.get(fp)
        if lock is None:
            lock = _exchange_locks[fp] = threading.Lock()
        return lock


_EXCHANGE_FAILURE_TTL_TRANSIENT_SECONDS = 60.0     # network blips: retry soon
_EXCHANGE_FAILURE_TTL_PERMANENT_SECONDS = 1800.0   # 401/403/404: won't heal
# The token itself is rejected — retrying with backoff just blocks the caller.
_EXCHANGE_PERMANENT_HTTP_STATUSES = frozenset({401, 403, 404})


def _token_fingerprint(raw_token: str) -> str:
    """Short fingerprint of a raw token for cache keying (avoids storing full token)."""
    return hashlib.sha256(raw_token.encode()).hexdigest()[:16]


def _read_jwt_store(path: Path) -> Optional[dict]:
    """Bounded read of the on-disk JWT store → dict, or None if missing/unusable (a store over
    the 1 MiB cap or non-dict can't balloon memory or get rewritten back out)."""
    if not path.exists():
        return None
    try:
        if path.stat().st_size > _JWT_DISK_MAX_BYTES:
            logger.debug("Persisted Copilot JWT store exceeds %d bytes; ignoring",
                         _JWT_DISK_MAX_BYTES)
            return None
        loaded = json.loads(path.read_text(encoding="utf-8-sig"))
        return loaded if isinstance(loaded, dict) else None
    except Exception as exc:
        logger.debug("Failed to read persisted Copilot JWT store: %s", exc)
        return None


def _jwt_disk_path() -> Optional[Path]:
    """Path to the on-disk exchanged-JWT cache (profile-aware), or None."""
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home()) / _JWT_DISK_FILENAME
    except Exception:
        return None


def _with_jwt_store(verb: str, op):
    """Run ``op(path, store_or_None)`` against the disk store; failures are logged, never raised."""
    path = _jwt_disk_path()
    if not path:
        return None
    try:
        return op(path, _read_jwt_store(path))
    except Exception as exc:
        logger.debug("Failed to %s Copilot JWT: %s", verb, exc)
        return None


def evict_cached_exchanged_token(raw_token: str) -> None:
    """Drop any cached exchanged JWT for ``raw_token`` (in-process + on-disk) — the runtime
    stale-credential recovery path for ``model_not_available_for_integrator`` 400s."""
    if not raw_token:
        return
    fp = _token_fingerprint(raw_token)
    _jwt_cache.pop(fp, None)
    # Eviction = "force a fresh exchange": the negative-cache entry must go too.
    _exchange_failure_cache.pop(fp, None)

    def _evict(path, store):
        if store is not None and fp in store:
            del store[fp]
            atomic_json_write(path, store, indent=None, mode=0o600)

    _with_jwt_store("evict cached", _evict)


def _load_jwt_from_disk(fp: str) -> Optional[tuple[str, float, Optional[str]]]:
    """Persisted exchanged JWT for ``fp`` → (api_token, expires_at, base_url), or None."""
    def _load(path, store):
        entry = (store or {}).get(fp)
        if not isinstance(entry, dict):
            return None
        api_token = entry.get("api_token", "")
        expires_at = float(entry.get("expires_at", 0) or 0)
        return (api_token, expires_at, entry.get("base_url")) if api_token and expires_at else None

    return _with_jwt_store("load persisted", _load)


def _save_jwt_to_disk(fp: str, api_token: str, expires_at: float, base_url: Optional[str]) -> None:
    """Persist an exchanged JWT (0o600), pruning expired entries."""
    def _save(path, store):
        now = time.time()
        kept = {
            k: v for k, v in (store or {}).items()
            if isinstance(v, dict) and float(v.get("expires_at", 0) or 0) > now}
        kept[fp] = {"api_token": api_token, "expires_at": expires_at, "base_url": base_url}
        atomic_json_write(path, kept, indent=None, mode=0o600)

    _with_jwt_store("persist", _save)


# urllib's ``timeout`` only bounds socket ops AFTER DNS; getaddrinfo ignores it, so a networkless
# Windows host can hang for minutes (observed: a 17-minute event-loop stall).
_DNS_GRACE_SECONDS = 5.0


def _urlopen_bounded(req, timeout: float):
    """urlopen() on a daemon thread, abandoned after timeout + _DNS_GRACE_SECONDS so a
    DNS/getaddrinfo hang cannot block the caller. Raises the worker's exception or TimeoutError."""
    box: dict = {}
    abandoned = threading.Event()

    def _worker() -> None:
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except BaseException as exc:  # re-raised on the caller's thread
            box["exc"] = exc
            return
        if abandoned.is_set():  # caller already timed out — release the socket
            with contextlib.suppress(Exception):
                resp.close()
            return
        box["resp"] = resp

    t = threading.Thread(target=_worker, name="copilot-token-exchange", daemon=True)
    t.start()
    t.join(timeout + _DNS_GRACE_SECONDS)
    if t.is_alive():
        abandoned.set()
        raise TimeoutError("copilot token exchange exceeded hard cap of "
                           f"{timeout + _DNS_GRACE_SECONDS:.0f}s (DNS/getaddrinfo hang?)")
    if "exc" in box:
        raise box["exc"]
    if "resp" not in box:
        raise TimeoutError("copilot token exchange worker died without result")
    return box["resp"]


def _fetch_exchange_with_retry(req, timeout: float, fp: str) -> dict:
    """GET the exchange with backoff for startup network races; raises ValueError on failure.

    Permanent rejections (401/403/404) skip the retry loop. Failures populate the negative
    cache (long TTL for permanent, short for transient); success clears it.
    """
    last_exc: Optional[Exception] = None
    permanent_failure = False
    for attempt in range(1, _EXCHANGE_MAX_ATTEMPTS + 1):
        try:
            with _urlopen_bounded(req, timeout) as resp:
                data = json.loads(resp.read().decode())
            _exchange_failure_cache.pop(fp, None)
            return data
        except Exception as exc:
            last_exc = exc
            status = getattr(exc, "code", None) or getattr(exc, "status", None)
            permanent_failure = status in _EXCHANGE_PERMANENT_HTTP_STATUSES
            if permanent_failure:
                logger.debug("Copilot token exchange rejected (HTTP %s); not retrying", status)
                break
            if attempt < _EXCHANGE_MAX_ATTEMPTS:
                sleep_s = _EXCHANGE_BACKOFF_BASE_SECONDS * attempt
                logger.debug("Copilot token exchange attempt %d/%d failed (%s); retrying in %.1fs",
                             attempt, _EXCHANGE_MAX_ATTEMPTS, exc, sleep_s)
                time.sleep(sleep_s)
    _exchange_failure_cache[fp] = time.time() + (
        _EXCHANGE_FAILURE_TTL_PERMANENT_SECONDS if permanent_failure
        else _EXCHANGE_FAILURE_TTL_TRANSIENT_SECONDS)
    raise ValueError(f"Copilot token exchange failed after {_EXCHANGE_MAX_ATTEMPTS} attempts: "
                     f"{last_exc}") from last_exc


def _cache_entry_fresh(cached) -> bool:
    return bool(cached) and time.time() < cached[1] - _JWT_REFRESH_MARGIN_SECONDS


def exchange_copilot_token(
    raw_token: str, *, timeout: float = 10.0) -> tuple[str, float, Optional[str]]:
    """Exchange a raw GitHub token for a Copilot API token → (token, expires_at, base_url).

    The token is a semicolon-separated string (not a JWT) used as a Bearer token. ``base_url``
    is the account-specific host: the exchange's ``endpoints.api`` (enterprise/proxied
    accounts), else derived from the token's ``proxy-ep``; individual accounts have neither,
    so it is None. Cached in-process until close to expiry. Raises ``ValueError`` on failure.
    """
    fp = _token_fingerprint(raw_token)
    # Fast paths outside the lock: a valid in-process JWT needs no exchange, and a recent failure
    # means queueing behind the in-flight holder (up to ~50 s) would only park an executor thread
    # to learn the same answer.
    cached = _jwt_cache.get(fp)
    if _cache_entry_fresh(cached):
        return cached
    _fail_until = _exchange_failure_cache.get(fp, 0.0)
    if time.time() < _fail_until:
        raise ValueError("Copilot token exchange recently failed; skipping re-attempt "
                         f"for another {int(_fail_until - time.time())}s")
    # Note: a waiter's own ``timeout`` is not honoured across the lock wait — by design of
    # single-flight, it observes the holder's outcome instead.
    with _exchange_lock_for(fp):
        return _exchange_copilot_token_locked(raw_token, fp, timeout=timeout)


def _exchange_copilot_token_locked(
    raw_token: str, fp: str, *, timeout: float) -> tuple[str, float, Optional[str]]:
    # Re-check in-process under the lock (a queued-behind caller may have just exchanged), then
    # on-disk: a fresh process may hold a still-valid persisted JWT, avoiding a network
    # round-trip precisely when the network is most likely flaky.
    for lookup in (_jwt_cache.get, _load_jwt_from_disk):
        cached = lookup(fp)
        if _cache_entry_fresh(cached):
            _jwt_cache[fp] = cached
            return cached
    # Negative cache: fail fast so provider discovery / picker opens don't block.
    _fail_until = _exchange_failure_cache.get(fp, 0.0)
    if time.time() < _fail_until:
        raise ValueError("Copilot token exchange recently failed; skipping re-attempt "
                         f"for another {int(_fail_until - time.time())}s")
    req = urllib.request.Request(
        _TOKEN_EXCHANGE_URL, method="GET",
        headers={"Authorization": f"token {raw_token}", "User-Agent": _EXCHANGE_USER_AGENT,
                 "Accept": "application/json", "Editor-Version": _EDITOR_VERSION})
    data = _fetch_exchange_with_retry(req, timeout, fp)
    api_token = data.get("token", "")
    if not api_token:
        raise ValueError("Copilot token exchange returned empty token")
    expires_at = float(data.get("expires_at") or 0) or time.time() + 1800
    # ``endpoints.api`` is authoritative (Copilot Enterprise / proxied accounts); else derive from
    # the token's ``proxy-ep``. Individual accounts have neither → None (registry default).
    endpoints = data.get("endpoints")
    base_url: Optional[str] = (
        str(endpoints.get("api") or "").strip().rstrip("/") if isinstance(endpoints, dict) else ""
    ) or _derive_base_url_from_proxy_ep(api_token)
    _jwt_cache[fp] = (api_token, expires_at, base_url)
    _save_jwt_to_disk(fp, api_token, expires_at, base_url)
    logger.debug("Copilot token exchanged, expires_at=%s, base_url=%s", expires_at, base_url)
    return api_token, expires_at, base_url


def _derive_base_url_from_proxy_ep(token: str) -> Optional[str]:
    """Copilot API base URL from the token's ``proxy-ep=proxy.<host>`` field (→ ``api.``)."""
    m = re.search(r'(?:^|;)\s*proxy-ep=([^;\s]+)', token)
    if not m:
        return None
    proxy_ep = re.sub(r"^https?://", "", m.group(1), count=1).rstrip("/")
    proxy_ep = re.sub(r"^proxy\.", "api.", proxy_ep, count=1)
    return f"https://{proxy_ep}"


def get_copilot_api_token(raw_token: str) -> tuple[str, Optional[str]]:
    """``(api_token, base_url)`` from the exchange, or ``(raw_token, None)`` when it fails
    (accounts that don't need exchange keep working)."""
    if not raw_token:
        return raw_token, None
    try:
        api_token, _, base_url = exchange_copilot_token(raw_token)
        return api_token, base_url
    except Exception as exc:
        logger.debug("Copilot token exchange failed, using raw token: %s", exc)
        return raw_token, None


# X-GitHub-Api-Version pins the Copilot API generation: without it /models serves legacy
# default-tier limits (claude-opus-5.5 as 200K, no billing tiers — issue #46), so long-context
# models resolve ~5x too small. GitHub REST and the Copilot backend use SEPARATE version
# namespaces that reject each other's dates — REST (api.github.com) 400s on this value, and the
# Copilot backend 400s "invalid apiVersion" on REST-supported dates (both directions verified
# live in hermes-agent#126897) — so never send it to the token exchange and never "fix" the date
# to a REST-supported value. Copilot exposes no version/deprecation headers and no versions
# endpoint, so the date cannot be discovered at runtime.
_COPILOT_API_VERSION = "2026-08-01"


def _copilot_api_version() -> str:
    """Pinned Copilot API date, overridable via ``HERMES_COPILOT_API_VERSION`` (empty → the pin).

    Fail-open: under the multiplex gateway get_secret raises by design when no profile scope is
    installed, and a header builder must not crash a turn over a compatibility date — the pin
    applies to every profile alike.
    """
    try:
        return (get_secret("HERMES_COPILOT_API_VERSION") or "").strip() or _COPILOT_API_VERSION
    except UnscopedSecretError:
        # The one designed raise on this path (multiplex on, no scope installed). The pin applies
        # to every profile alike, so fail open; anything else is an infrastructure fault the token
        # read on the same path already lets propagate.
        logger.debug("HERMES_COPILOT_API_VERSION: unscoped secret read; using pinned date",
                     exc_info=True)
        return _COPILOT_API_VERSION


def copilot_request_headers(
    *, is_agent_turn: bool = True, is_vision: bool = False) -> dict[str, str]:
    """Build the standard headers for Copilot API requests."""
    headers: dict[str, str] = {"Editor-Version": _EDITOR_VERSION, "User-Agent": "HermesAgent/1.0",
                               "X-GitHub-Api-Version": _copilot_api_version(),
                               "Copilot-Integration-Id": "vscode-chat",
                               "Openai-Intent": "conversation-edits",
                               "x-initiator": "agent" if is_agent_turn else "user"}
    if is_vision:
        headers["Copilot-Vision-Request"] = "true"
    return headers


# ── ProviderProfile credential seams ────────────────────────────────────────────────────────────

# Warn once per token per process when the exchange degrades to the raw token (#114740).
_RAW_DEGRADATION_WARNED: set[str] = set()


def _warn_raw_degradation_once(token: str) -> None:
    """WARN once per token per process when Copilot exchange degrades to raw token (#114740)."""
    fingerprint = _token_fingerprint(token) if token else "unknown"
    if fingerprint in _RAW_DEGRADATION_WARNED:
        return
    _RAW_DEGRADATION_WARNED.add(fingerprint)
    logger.warning(
        "Copilot token exchange degraded to RAW token (exchange "
        "unavailable); enterprise-only models may 400 with "
        "model_not_available_for_integrator until exchange recovers."
    )


def _reset_raw_degradation_warned() -> None:
    """Clear the degradation warning cache (for test isolation)."""
    _RAW_DEGRADATION_WARNED.clear()


# Every pool source the same token can be seeded under (one per env var).
_POOL_SOURCES = tuple(f"env:{v}" for v in COPILOT_ENV_VARS)

_STALE_CREDENTIAL_MARKERS = (
    "model_not_available_for_integrator",
    "not available for integrator",
    "model_not_supported",
    "the requested model is not supported",
)

_GITHUB_MODELS_FREE_TIER_HOST = "models.inference.ai.azure.com"
_GITHUB_MODELS_HINT = (
    "   💡 GitHub Models free tier (models.inference.ai.azure.com) caps every",
    "      request at ~8K tokens. Hermes' system prompt + tool schemas baseline",
    "      exceeds that floor, so this endpoint cannot run an agentic loop.",
    "      Use the `copilot` provider with a Copilot subscription token (`hermes",
    "      setup` → GitHub Copilot), or pick any other provider.",
)


def _refresh_exchanged_token() -> Optional[tuple[str, Optional[str]]]:
    """401 on the main client: the raw GitHub token is stable, the short-TTL *exchanged* IDE token is
    what expires mid-turn (``401 IDE token expired``), so force a fresh exchange; fall back to the
    raw token only if the exchange itself is unavailable."""
    try:
        new_token, token_source = resolve_copilot_token()
    except Exception as exc:  # health: allow BLE001 -- moved verbatim from core (hermes_cli/copilot_auth.py, credential_pool, client_lifecycle, auth.py)
        logger.debug("Copilot credential refresh failed: %s", exc)
        return None
    new_token, enterprise_base_url = (new_token.strip() if isinstance(new_token, str) else ""), None
    if not new_token:
        return None
    try:
        evict_cached_exchanged_token(new_token)
        api_token, exchanged_base_url = get_copilot_api_token(new_token)
        if isinstance(api_token, str) and api_token.strip():
            new_token, enterprise_base_url = api_token.strip(), exchanged_base_url
    except Exception as exc:  # health: allow BLE001 -- moved verbatim from core (hermes_cli/copilot_auth.py, credential_pool, client_lifecycle, auth.py)
        logger.debug("Copilot 401 re-exchange failed, using resolved token: %s", exc)
    logger.info("Copilot credentials refreshed from %s", token_source)
    return new_token, enterprise_base_url


def _reexchange_stale_token() -> Optional[tuple[str, Optional[str]]]:
    """Stale-credential 400 (``model_not_available_for_integrator``): typically a raw ``ghu_`` token
    cached when the startup exchange degraded. Evict + re-exchange; None when still degraded (a
    rebuild would not help, so the caller's single-shot retry is not burned)."""
    try:
        raw_token, token_source = resolve_copilot_token()
        if not isinstance(raw_token, str) or not raw_token.strip():
            return None
        raw_token = raw_token.strip()
        evict_cached_exchanged_token(raw_token)
        api_token, enterprise_base_url = get_copilot_api_token(raw_token)
    except Exception as exc:  # health: allow BLE001 -- moved verbatim from core (hermes_cli/copilot_auth.py, credential_pool, client_lifecycle, auth.py)
        logger.debug("Copilot stale-credential recovery failed: %s", exc)
        return None
    if not isinstance(api_token, str) or not api_token.strip():
        return None
    if api_token == raw_token and not enterprise_base_url:
        logger.warning(
            "Copilot stale-credential recovery: exchange still degraded to raw token; skipping retry "
            "(network/exchange endpoint unavailable)."
        )
        return None
    logger.info("Copilot credentials re-exchanged after stale-credential 400 (source=%s)", token_source)
    return api_token.strip(), enterprise_base_url


def _revalidate_in_memory_token() -> Optional[tuple[str, Optional[str]]]:
    """Auxiliary-client 401: drop only the in-process JWT and exchange again (a persisted, still-valid
    JWT and the negative cache are honoured); raises when the exchange fails."""
    raw_token, _source = resolve_copilot_token()
    if not str(raw_token or "").strip():
        return None
    _jwt_cache.pop(_token_fingerprint(raw_token), None)
    api_token, _expires_at, base_url = exchange_copilot_token(raw_token)
    return api_token, base_url


_RUNTIME_REFRESHERS = {
    "unauthorized": _refresh_exchanged_token,
    "stale": _reexchange_stale_token,
    "auxiliary_unauthorized": _revalidate_in_memory_token,
}


class CopilotCredentialsMixin:
    """Fills the generic credential seams of ``providers.base.ProviderProfile`` for Copilot."""

    def resolve_api_key_secret(self) -> tuple[str, str]:
        """The exchanged Copilot token. A home that never chose Copilot gets the raw env token (it
        still reads as present) and GitHub is not called: provider listings resolve this for every
        provider with a token in reach (``activation``)."""
        from .activation import github_calls_allowed

        try:
            token, source = resolve_copilot_token()
            if token and not github_calls_allowed():
                return token, source
            if token:
                api_token, _base_url = get_copilot_api_token(token)
                return api_token, source
        except Exception:  # health: allow BLE001 S110 -- moved verbatim from core (hermes_cli/copilot_auth.py, credential_pool, client_lifecycle, auth.py)
            pass
        return "", ""

    def runtime_base_url(self, *, api_key: str, default: str, env_url: str) -> str:
        """The API base comes from the token-exchange response (endpoints.api, proxy-ep fallback),
        authoritative for Enterprise / proxied accounts; falls back to the registry default."""
        from .activation import github_calls_allowed

        base_url = env_url.rstrip("/") if env_url else default
        try:
            raw_token, _ = resolve_copilot_token()
            if raw_token and github_calls_allowed():
                resolved = (get_copilot_api_token(raw_token)[1] or "").strip()
                if resolved:
                    base_url = resolved
        except Exception as exc:  # health: allow BLE001 -- moved verbatim from core (hermes_cli/copilot_auth.py, credential_pool, client_lifecycle, auth.py)
            logger.debug("Copilot base URL resolution fell back to default: %s", exc)
        return base_url

    def prepare_pool_entry(self, entry: Any, api_key: str) -> str:
        """A pool row may still carry the RAW GitHub token: the seeder skips the exchange while
        copilot is merely discovered (ambient gh login, not in config). Here copilot IS the runtime
        target, and a raw token routes to the language-server integrator whose allowlist omits
        enterprise-only models (400 model_not_available_for_integrator)."""
        if not api_key or not validate_copilot_token(api_key)[0]:
            return api_key  # already an exchanged API token
        api_token, enterprise_base_url = get_copilot_api_token(api_key)
        if api_token == api_key and not enterprise_base_url:
            _warn_raw_degradation_once(api_key)
            return api_key
        entry.access_token = api_token
        if enterprise_base_url:
            entry.base_url = enterprise_base_url
        return api_token

    def seed_pool_credentials(self, *, suppressed_sources: frozenset,
                              explicitly_configured: bool) -> list[dict]:
        # Env tokens are resolved dynamically; they don't live in the auth store. All-sources gate
        # BEFORE any work: the exchange retries 3x with backoff (~35s worst case); a user who
        # suppressed every copilot source must not pay that on every pool load.
        try:
            if all(s in suppressed_sources for s in _POOL_SOURCES):
                return []
            token, source = resolve_copilot_token()
            if not token:
                return []
            source_name = f"env:{source}"
            if source_name in suppressed_sources:  # per-source gate BEFORE the network exchange
                return []
            from .activation import github_calls_allowed

            # Core's ``explicitly_configured`` counts any profile env var, so a GH_TOKEN kept for
            # other tools would pass it; the plugin's own check does not (``activation``).
            del explicitly_configured
            if not github_calls_allowed():
                # Only discovered here (an ambient env token), not selected anywhere: no model is
                # routed to it, so the exchange — and its degradation warning on every pool load —
                # buys nothing (#114740). Seed the raw token; selecting copilot re-seeds and exchanges.
                api_token, enterprise_base_url = token, None
            else:
                api_token, enterprise_base_url = get_copilot_api_token(token)
                # A failed exchange falls back to the RAW token, which the Copilot API routes to the
                # "copilot-language-server" integrator whose allowlist omits enterprise-only models
                # -> HTTP 400 on every turn. Surface it once.
                if api_token == token and not enterprise_base_url:
                    _warn_raw_degradation_once(token)
            return [{"source": source_name, "access_token": api_token, "base_url": enterprise_base_url,
                     "label": source}]
        except Exception as exc:  # health: allow BLE001 -- moved verbatim from core (hermes_cli/copilot_auth.py, credential_pool, client_lifecycle, auth.py)
            logger.debug("Copilot token seed failed: %s", exc)
            return []

    def remove_credential_source(self, source: str) -> Optional[tuple[tuple[str, ...], tuple[str, ...]]]:
        """The same token can be seeded under several env:<VAR> rows, so suppress every variant or the
        duplicates resurrect the entry. Shell state is left untouched."""
        if not source.startswith("env:"):
            return None
        return _POOL_SOURCES, (
            "Suppressed all copilot env token sources — they will not be re-seeded.",
            "Note: Your shell environment is unchanged.",
            "Run `hermes auth add copilot` to re-enable if needed.",
        )

    def refresh_runtime_credentials(self, *, reason: str) -> Optional[tuple[str, Optional[str]]]:
        refresher = _RUNTIME_REFRESHERS.get(reason)
        return refresher() if refresher else None

    def credential_refresh_notice(self, *, reason: str) -> str:
        if reason == "stale":
            return "Copilot credential re-exchanged after model_not_available 400. Retrying request..."
        return "Copilot credentials refreshed after 401. Retrying request..." if reason == "unauthorized" else ""

    def is_stale_credential_error(self, *, status_code: Optional[int], message: str) -> bool:
        """A 400 that is really a STALE / DEGRADED credential (status 400 AND an integrator /
        model-not-supported marker, so a wrong model name never triggers the re-exchange)."""
        lowered = (message or "").lower()
        if status_code != 400 and "error code: 400" not in lowered:
            return False
        return any(marker in lowered for marker in _STALE_CREDENTIAL_MARKERS)

    def request_rejection_hint(self, *, status_code: Optional[int], base_url: str) -> tuple[str, ...]:
        """GitHub Models free tier caps requests at 8K tokens, under the system prompt + tool schema
        floor; compression can't help, so say so."""
        from .helpers import base_url_host_matches
        if status_code == 413 and base_url_host_matches(base_url, _GITHUB_MODELS_FREE_TIER_HOST):
            return _GITHUB_MODELS_HINT
        return ()

    def missing_credentials_hint(self) -> str:
        """Appended to core's "No usable credentials found" message: since the rule-11 cut a
        `gh auth login` alone no longer signs Hermes in, so name the plugin's own login. The env vars
        stay out: core's runtime message already lists them right before this hint."""
        return "Run `hermes auth add copilot` to sign in."


def copilot_cli_auth_evidence() -> tuple[bool, Optional[str]]:
    """Positive sign-in evidence for copilot-acp without spawning the CLI: a supported env token (the
    same vars the Copilot CLI honors). The CLI's own token files are not read (catalog rule 11), so a
    CLI-only login reads as "managed by the GitHub Copilot CLI", never as signed out."""
    try:
        for env_var in COPILOT_ENV_VARS:
            val = (get_secret(env_var) or "").strip()
            if val and validate_copilot_token(val)[0]:
                return True, f"env: {env_var}"
    except Exception as exc:  # health: allow BLE001 -- moved verbatim from core (hermes_cli/copilot_auth.py, credential_pool, client_lifecycle, auth.py)
        logger.debug("copilot-acp env token evidence check failed: %s", exc)
    return False, None
