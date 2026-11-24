"""DingTalk plugin registration: setup row, configured check, standalone sender, ``register``."""

import dataclasses
import importlib
import importlib.machinery
import logging
from typing import Any

# Relative imports: the loader runs this package as ``hermes_plugins.<slug>``, so ``.adapter`` is the one
# module (and the one ``DingTalkAdapter`` class object) the registry and the standalone sender share.
from . import gateway_env
from .adapter import (
    PLATFORM_NAME, DingTalkAdapter, _apply_yaml_config, _is_connected, _standalone_send, dingtalk_deps_present,
    ensure_dingtalk_deps, interactive_setup,
)

logger = logging.getLogger(__name__)

_INSTALL_HINT = "DingTalk SDK missing: reinstall the plugin (hermes plugins update dingtalk) to restore its dependencies"

# PlatformEntry fields newer than some cores this plugin may load on (PlatformEntry raises TypeError on
# unknown keys), so each is passed only when the running core has it.
_OPTIONAL_ENTRY_KWARGS: dict[str, Any] = {
    "dashboard_description": "Connect Hermes to DingTalk groups (钉钉).",
    "docs_url": "https://open.dingtalk.com/document/orgapp/the-robot-development-process",
    "dashboard_env_vars": ("DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET"),
    "ensure_deps_fn": ensure_dingtalk_deps,
    "display_tier": "low",
    "env_enable_credentials": gateway_env.ENV_ENABLE_CREDENTIALS,
    **gateway_env.AUTHZ,
}


def _supported_entry_fields() -> set:
    from gateway.platform_registry import PlatformEntry
    return {f.name for f in dataclasses.fields(PlatformEntry)}


def _standalone_send_kwargs(supported: set) -> dict[str, Any]:
    """Text sends go straight to the robot webhook (``gateway.platforms.send_targets`` is core's
    send-contract module for platform plugins; a core without the field gets the plain sender)."""
    if "standalone_send" not in supported:
        return {}
    from gateway.platforms.send_targets import StandaloneSendPolicy
    return {"standalone_send": StandaloneSendPolicy(text_direct=True)}


def platform_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = dict(
        name=PLATFORM_NAME, label="DingTalk", adapter_factory=DingTalkAdapter, check_fn=dingtalk_deps_present,
        is_connected=_is_connected, validate_config=_is_connected,
        required_env=["DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET"], install_hint=_INSTALL_HINT,
        setup_fn=interactive_setup, apply_yaml_config_fn=_apply_yaml_config, cron_deliver_env_var="DINGTALK_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send, emoji="🐳", allow_update_command=True,
        env_enablement_fn=gateway_env.env_enablement,
    )
    supported = _supported_entry_fields()
    kwargs.update(_standalone_send_kwargs(supported))
    for key, value in _OPTIONAL_ENTRY_KWARGS.items():
        if key in supported:
            kwargs[key] = value
        else:
            logger.debug("dingtalk: core PlatformEntry has no %r field; not passing it", key)
    return kwargs


def core_ships_dingtalk() -> bool:
    """True on a Hermes core that still bundles DingTalk (``plugins/platforms/dingtalk``).

    Looks only inside the imported package's own ``__path__``: a plain ``find_spec`` also consults meta-path
    finders (e.g. an editable install of a different checkout) and can report a module the running core
    does not have."""
    try:
        search = list(getattr(importlib.import_module("plugins.platforms"), "__path__", None) or ())
    except ImportError:
        return False
    spec = importlib.machinery.PathFinder.find_spec("plugins.platforms.dingtalk", search) if search else None
    # ``origin`` is None for a namespace match: a directory left behind holding only ``__pycache__``
    # after an update deleted the bundled files is not a core that ships DingTalk.
    return spec is not None and spec.origin is not None


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin loader."""
    if core_ships_dingtalk():
        # Registering on top of the built-in would replace core's adapter.
        logger.info("dingtalk: this Hermes core still ships DingTalk built in; "
                    "the plugin stays inactive until core drops it")
        return
    ctx.register_platform(**platform_kwargs())
