"""inbox: ``hermes inbox`` and the ``/inbox`` slash command.

Registers one CLI command (``ctx.register_cli_command``) and one in-session
slash command (``ctx.register_command``) through the public plugin surface.
No model tools, no hooks, nothing runs during chats.
"""

from __future__ import annotations


def _handle_inbox(raw_args: str) -> str:
    """Handler for /inbox — called with everything after the command name.

    Returns the read-only inbox report as plain text, truncated under 4096 chars.
    """
    # Imported here, not at module top: pytest imports this file bare (no parent package)
    # when it collects the plugin directory.
    from .report import build_inbox, render_inbox

    text = render_inbox(build_inbox())
    if len(text) >= 4096:
        text = text[:4080].rstrip() + "\n… (truncated)"
    return text


def register(ctx) -> None:
    # Imported here, not at module top: pytest imports this file bare (no parent package)
    # when it collects the plugin directory.
    from .cli import DESCRIPTION, HELP, handle, setup_parser

    ctx.register_cli_command(
        "inbox", help=HELP, setup_fn=setup_parser, handler_fn=handle, description=DESCRIPTION,
    )
    ctx.register_command(
        "inbox",
        handler=_handle_inbox,
        description="Read-only overview of everything in flight: stalled/running work, cron, kanban, sessions",
        args_hint="",
    )
