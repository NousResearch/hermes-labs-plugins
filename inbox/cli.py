"""``hermes inbox`` argparse wiring."""

from __future__ import annotations

import argparse
import json

HELP = "Show everything in flight: background work, results, cron, sessions, kanban"
DESCRIPTION = (
    "View Hermes activity without changing native task stores: what needs your attention "
    "(stalled or dropped delegations, failed cron runs, orphaned background processes, blocked "
    "kanban tasks), what is in progress, finished delegation results not yet delivered, upcoming "
    "cron jobs and open chat surfaces. Explicit observe, mark-seen, ack and snooze actions "
    "write only Inbox-owned local state; acknowledged or snoozed items stay listed and marked "
    "unless --hide-acked or --hide-snoozed is given. --fleet adds same-host Herald observations."
)


def setup_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Print the snapshot as JSON")
    parser.add_argument("action", nargs="?", default="view", choices=("view", "observe", "changes", "mark-seen", "ack", "snooze"))
    parser.add_argument("occurrence", nargs="?", type=int, help="Local observation occurrence for ack/snooze")
    parser.add_argument("--seconds", type=float, help="Explicit snooze duration, at most seven days")
    parser.add_argument("--consumer", default="owner", help="Independent observation consumer")
    parser.add_argument("--through", type=int, help="Explicit observed sequence to mark seen")
    parser.add_argument("--fleet", action="store_true",
                        help="Include same-host profile Herald ledgers (opt-in, disabled by default)")
    # Acknowledged/snoozed items are shown and marked by default; only these explicit filters hide them.
    parser.add_argument("--hide-acked", action="store_true",
                        help="Hide items acknowledged locally (shown and marked local=acked by default)")
    parser.add_argument("--hide-snoozed", action="store_true",
                        help="Hide items snoozed locally (shown and marked local=snoozed by default)")


def handle(args: argparse.Namespace) -> int:
    from .report import build_inbox, render_inbox

    action = getattr(args, "action", "view")
    if action != "view":
        from hermes_constants import get_hermes_home
        from .herald import profile_identity
        from .state import InboxState, InboxStateError
        from .observations import records
        import sqlite3
        from hermes_yaml import YAMLError
        try:
            home = get_hermes_home()
            state = InboxState(home, profile_identity(home))
            consumer = getattr(args, "consumer", "owner")
            if action == "observe":
                sequences = state.observe(records(build_inbox(home=home, fleet=bool(getattr(args, "fleet", False)))))
                result = {"created": len(sequences), "sequences": sequences}
            elif action == "changes":
                result = state.changes(consumer=consumer)
            elif action == "mark-seen":
                result = {"cursor": state.mark_seen(consumer=consumer, upto=getattr(args, "through", None))}
            elif action in ("ack", "snooze"):
                from .attention import AttentionState
                attention = AttentionState(state)
                occurrence = getattr(args, "occurrence", None)
                if occurrence is None:
                    raise InboxStateError("an observed occurrence is required")
                if action == "ack":
                    result = attention.ack(occurrence)
                else:
                    seconds = getattr(args, "seconds", None)
                    if seconds is None:
                        raise InboxStateError("an explicit snooze duration is required")
                    result = attention.snooze(occurrence, seconds)
            else:
                raise ValueError("unknown action")
            print(json.dumps(result, indent=2))
            return 0
        except (InboxStateError, ValueError, OSError, sqlite3.Error, YAMLError, RecursionError):
            print(json.dumps({"error": "Local Inbox state unavailable or invalid action input"}))
            return 2
    data = build_inbox(fleet=bool(getattr(args, "fleet", False)),
                       hide_acked=bool(getattr(args, "hide_acked", False)),
                       hide_snoozed=bool(getattr(args, "hide_snoozed", False)))
    print(json.dumps(data, indent=2, default=str) if args.json else render_inbox(data))
    return 0