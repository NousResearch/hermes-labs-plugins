"""GitHub Copilot model providers: ``copilot`` (Copilot API) and ``copilot-acp`` (Copilot CLI over ACP).

Hermes imports this file through provider discovery (``kind: model-provider``), never through
``register(ctx)``. Both profiles register here, once, and only on a core that no longer bundles
Copilot and carries the generic provider seams the profiles fill (hermes-agent#144788). On an
older core this plugin registers nothing and logs one warning: that core still serves Copilot
itself, and shadowing its profiles with ones its call sites cannot drive would break them.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

PROVIDER_NAMES = ("copilot", "copilot-acp")
# ProviderProfile members the profiles override or set. A core without them drives Copilot
# through its own name-keyed branches, so these profiles would be half-wired there.
_REQUIRED_SEAMS = (
    "api_mode_for_model", "client_headers", "request_headers", "max_tokens_param",
    "seed_pool_credentials", "refresh_runtime_credentials", "external_auth_evidence",
    "live_context_length", "replay_message_carriers", "start_device_login", "picker_catalog",
    "prepare_pool_entry", "refresh_pool_runtime_credentials", "credential_pool_base_urls",
)


def _bundled_copilot_dirs() -> list[Path]:
    """Copilot plugin dirs the running core ships, found the way core's discovery finds them
    (``providers/__init__.py``: ``<core>/plugins/model-providers/<name>/__init__.py``). Anchored on
    the ``providers`` module actually imported, so an editable install of another checkout can't
    answer for it; a leftover dir holding only ``__pycache__`` does not count."""
    import providers

    root = Path(providers.__file__).resolve().parent.parent / "plugins" / "model-providers"
    return [root / name for name in PROVIDER_NAMES if (root / name / "__init__.py").is_file()]


def inert_reason() -> str | None:
    """Why this core must not get these profiles, or None when it should."""
    from providers import base

    if bundled := _bundled_copilot_dirs():
        return f"this Hermes core still bundles GitHub Copilot ({bundled[0]})"
    if not hasattr(base, "PoolRuntimeCredential"):
        return "this Hermes core lacks the PoolRuntimeCredential provider SDK"
    if missing := [name for name in _REQUIRED_SEAMS if not hasattr(base.ProviderProfile, name)]:
        return f"this Hermes core lacks the provider seams the plugin needs ({', '.join(missing)})"
    return None


def _register() -> bool:
    reason = inert_reason()
    if reason:
        logger.warning("github-copilot plugin is inactive: %s. Update Hermes before using this "
                       "plugin revision.", reason)
        return False
    from providers import register_provider

    from .copilot import copilot
    from .copilot_acp import copilot_acp

    register_provider(copilot)
    register_provider(copilot_acp)
    return True


# pytest imports this file bare (no parent package) while collecting tests/; registering then
# would need the relative imports above, and the suite loads the package itself (tests/conftest.py).
ACTIVE = _register() if __package__ else False
