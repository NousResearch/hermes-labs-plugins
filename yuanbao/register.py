"""Yuanbao plugin registration: setup row, configured check, standalone sender, ``register``."""

import dataclasses
import functools
import importlib
import importlib.machinery
import logging
import re
from pathlib import Path
from typing import Any

from gateway.platforms._shared import config_connected_extra, get_scoped_secret, send_error

# Relative import: the loader runs this package as ``hermes_plugins.<slug>``, so ``.adapter`` is the one
# module (and the one ``YuanbaoAdapter`` class object) the registry, ``tools.py`` and the standalone
# sender all share.
from . import gateway_env
from .adapter import PLATFORM_NAME, WEBSOCKETS_AVAILABLE, YuanbaoAdapter
from .tools import register_tools

logger = logging.getLogger(__name__)


_INSTALL_HINT = "Yuanbao: websockets not installed. Run: pip install websockets"

_SETUP_PLATFORM = {
    "key": "yuanbao", "label": "Yuanbao", "emoji": "💎", "token_var": "YUANBAO_APP_ID",
    "setup_instructions": [
        "1. Download the Yuanbao app from https://yuanbao.tencent.com/",
        "2. In the app, go to PAI → My Bot and create a new bot",
        "3. After the bot is created, copy the App ID and App Secret",
        "4. Enter them below and Hermes will connect automatically over WebSocket",
    ],
    "vars": [
        {"name": "YUANBAO_APP_ID", "prompt": "App ID", "password": False,
         "help": "The App ID from your Yuanbao IM Bot credentials."},
        {"name": "YUANBAO_APP_SECRET", "prompt": "App Secret", "password": True,
         "help": "The App Secret (used for HMAC signing) from your Yuanbao IM Bot."},
    ],
}


def check_yuanbao_requirements() -> bool:
    """Runtime deps present (the websockets client)."""
    return WEBSOCKETS_AVAILABLE


def interactive_setup() -> None:
    """``hermes gateway setup`` flow: the App ID / App Secret prompts core's standard platform form ran,
    through the public setup helpers."""
    from hermes_cli.cli_output import print_info, print_success, print_warning
    from hermes_cli.gateway_setup_wizard import confirm_reconfigure, print_setup_header, save_env_values
    from hermes_cli.setup import prompt

    row = _SETUP_PLATFORM
    print_setup_header(f"{row['emoji']} {row['label']}")
    print()
    for line in row["setup_instructions"]:
        print_info(f"  {line}")
    if not confirm_reconfigure(row["label"], row["token_var"]):
        return
    for var in row["vars"]:
        print()
        print_info(f"  {var['help']}")
        value = prompt(f"  {var['prompt']}", password=var["password"])
        if value:
            save_env_values(**{var["name"]: value})
            print_success(f"  Saved {var['name']}")
        elif var["name"] == row["token_var"]:
            print_warning(f"  Skipped — {row['label']} won't work without this.")
            return
        else:
            print_info("  Skipped (can configure later)")
    print()
    print_success(f"{row['emoji']} {row['label']} configured!")


def _is_connected(config) -> bool:
    """Configured = App ID (or legacy App Key) + App Secret, from config extras or env."""
    extra = getattr(config, "extra", {}) or {}
    app_id = (extra.get("app_id") or get_scoped_secret("YUANBAO_APP_ID", "")
              or get_scoped_secret("YUANBAO_APP_KEY", "") or "")
    secret = extra.get("app_secret") or get_scoped_secret("YUANBAO_APP_SECRET", "") or ""
    return bool(str(app_id).strip() and str(secret).strip())


async def _standalone_send(pconfig, chat_id, message, *, thread_id=None, media_files=None, **_kwargs):
    """standalone_sender_fn: send via the running adapter's persistent WebSocket (no throwaway
    client possible — Yuanbao needs the live signed connection)."""
    adapter = YuanbaoAdapter.get_active()
    if adapter is None:
        return send_error("Yuanbao adapter is not running. Start the gateway with yuanbao platform enabled first.")
    try:
        return await adapter._outbound.sender.send_direct(chat_id, message, media_files)
    except Exception as e:
        logger.debug("Yuanbao standalone send failed", exc_info=True)
        return send_error(f"Yuanbao send failed: {e}")


# System-prompt platform hint (PlatformEntry.platform_hint); cached-prompt bytes, keep stable (core's
# Yuanbao hint verbatim). The DM guidance core's session notes carried lives in the yb_send_dm schema.
_PLATFORM_HINT = (
    "You are on Yuanbao (\u817e\u8baf\u5143\u5b9d), a Chinese AI assistant "
    "platform. Markdown renders (code blocks, tables, bold/italic). "
    "You can send files natively: write MEDIA:/absolute/path/to/file in your response. "
    "Images (.jpg, .png, .webp, .gif) send as photos, other files as downloadable documents (max 50 MB); "
    "image URLs via ![alt](url) are downloaded and sent as photos. Never claim you lack file-sending. "
    "Stickers (\u8d34\u7eb8/\u8868\u60c5\u5305): when the user sends one (you see '[emoji: "
    "\u540d\u79f0]') or asks for one, use the sticker tools \u2014 yb_search_sticker with a Chinese "
    "keyword, then yb_send_sticker with the chosen id \u2014 which send a real native sticker. Never "
    "draw sticker-like PNGs and send them as images, and bare Unicode emoji is not a substitute."
)
SKILL_NAME = "yuanbao"
SKILL_DESCRIPTION = "Yuanbao (元宝) groups: @mention users, query info/members."
_SEND_TARGET_RE = re.compile(r"^\s*((?:group|direct):[^:]+)\s*$")


def _parse_send_target(ref, not_explicit=None):
    # "group:<code>" / "direct:<id>"; a bare number is a group code. Anything else is ``not_explicit``:
    # core's ``NOT_EXPLICIT`` sentinel for ``target_ref_syntax_fn`` (never the generic id rules), None for
    # the older ``parse_target_ref_fn`` seam (None = fall through).
    match = _SEND_TARGET_RE.fullmatch(ref)
    if match:
        return match.group(1), None
    return (f"group:{ref.strip()}", None) if ref.strip().isdigit() else not_explicit


def _send_target_kwargs(supported: set) -> dict[str, Any]:
    """send_message wiring: the native target syntax and the standalone-send policy (media and text sends
    use ``_standalone_send``, which needs the live signed WebSocket). ``gateway.platforms.send_targets`` is
    core's send-contract module for platform plugins; a core without it gets the older parser seam."""
    if "target_ref_syntax_fn" in supported and "standalone_send" in supported:
        from gateway.platforms.send_targets import NOT_EXPLICIT, StandaloneSendPolicy
        return {"target_ref_syntax_fn": functools.partial(_parse_send_target, not_explicit=NOT_EXPLICIT),
                "standalone_send": StandaloneSendPolicy(label="Yuanbao", media=True, text_direct=True)}
    return {"parse_target_ref_fn": _parse_send_target} if "parse_target_ref_fn" in supported else {}


# PlatformEntry fields newer than some cores this plugin may load on (PlatformEntry raises TypeError on
# unknown keys), so each is passed only when the running core has it.
_OPTIONAL_ENTRY_KWARGS: dict[str, Any] = {
    "dashboard_label": "Yuanbao (元宝)",
    "dashboard_description": "Connect Hermes to Tencent Yuanbao.",
    "dashboard_required_env": (),
    "config_connected_fn": config_connected_extra("app_id", "app_secret"),
    "env_enable_credentials": gateway_env.ENV_ENABLE_CREDENTIALS,
    **gateway_env.AUTHZ,
}


def _supported_entry_fields() -> set:
    from gateway.platform_registry import PlatformEntry
    return {f.name for f in dataclasses.fields(PlatformEntry)}


def platform_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = dict(
        name=PLATFORM_NAME, label="Yuanbao", adapter_factory=YuanbaoAdapter,
        check_fn=check_yuanbao_requirements, is_connected=_is_connected,
        required_env=["YUANBAO_APP_ID", "YUANBAO_APP_SECRET"], install_hint=_INSTALL_HINT,
        setup_fn=interactive_setup, cron_deliver_env_var="YUANBAO_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send, emoji="💎", allow_update_command=False,
        platform_hint=_PLATFORM_HINT, env_enablement_fn=gateway_env.env_enablement,
    )
    supported = _supported_entry_fields()
    kwargs.update(_send_target_kwargs(supported))
    for key, value in _OPTIONAL_ENTRY_KWARGS.items():
        if key in supported:
            kwargs[key] = value
        else:
            logger.debug("yuanbao: core PlatformEntry has no %r field; not passing it", key)
    return kwargs


def core_ships_yuanbao() -> bool:
    """True on a Hermes core that still bundles Yuanbao (``plugins/platforms/yuanbao`` or
    ``tools/yuanbao_tools.py``, removed together when core dropped it).

    Looks only inside the imported packages' own ``__path__``: a plain ``find_spec`` also consults meta-path
    finders (e.g. an editable install of a different checkout) and can report a module the running core
    does not have."""
    def _ships(package: str, module: str) -> bool:
        try:
            search = list(getattr(importlib.import_module(package), "__path__", None) or ())
        except ImportError:
            return False
        spec = importlib.machinery.PathFinder.find_spec(module, search) if search else None
        # ``origin`` is None for a namespace match: a directory left behind holding only ``__pycache__``
        # after an update deleted the bundled files is not a core that ships Yuanbao.
        return spec is not None and spec.origin is not None

    return _ships("plugins.platforms", "plugins.platforms.yuanbao") or _ships("tools", "tools.yuanbao_tools")


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin loader."""
    if core_ships_yuanbao():
        # Registering on top of the built-in would shadow core's yb_* tools and replace its adapter.
        logger.info("yuanbao: this Hermes core still ships Yuanbao built in; "
                    "the plugin stays inactive until core drops it")
        return
    ctx.register_platform(**platform_kwargs())
    register_tools(ctx)
    # Formerly core's optional ``yuanbao`` skill (@mention, DM and member-lookup workflows for the yb_* tools).
    ctx.register_skill(SKILL_NAME, Path(__file__).resolve().parent / "skills" / SKILL_NAME / "SKILL.md",
                       description=SKILL_DESCRIPTION)
