"""inbox: ``hermes inbox``, one read-only view of everything in flight.

Registers one CLI command through the public plugin surface (``ctx.register_cli_command``).
No model tools, no hooks, nothing runs during chats.
"""

from __future__ import annotations


def register(ctx) -> None:
    # Imported here, not at module top: pytest imports this file bare (no parent package)
    # when it collects the plugin directory.
    from .cli import DESCRIPTION, HELP, handle, setup_parser

    ctx.register_cli_command(
        "inbox", help=HELP, setup_fn=setup_parser, handler_fn=handle, description=DESCRIPTION,
    )
