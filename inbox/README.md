# Inbox

A snapshot of work in flight, with optional same-host Herald work threads and
explicit local observation, cursor, acknowledgment and snooze actions. Native
task stores remain read-only; only explicit actions write Inbox-owned state.

This remains an experimental [Hermes Labs](../README.md) plugin, not a supported
first-party product. The [MIT license](LICENSE) and original attribution remain.

## Installation

`hermes plugins install inbox` installs the existing reviewed catalog pin. New
repository changes reach that command only after a separate catalog SHA-bump PR
is reviewed and merged. The observation/attention commands below require the
0.2.0 candidate; their presence in repository docs does not update an older pin.
For source qualification, use the native installer with this repository, subdir
`inbox`, and an explicitly reviewed full commit SHA. No second plugin ID or CLI
command is introduced.

## Implemented commands

```text
hermes inbox                  active-profile snapshot
hermes inbox --json           structured snapshot
hermes inbox --fleet           opt-in same-host Herald ledger view
hermes inbox --fleet --json    structured fleet observations and coverage
hermes -p work inbox           selected profile; plugin must be installed there
hermes inbox observe --fleet   explicitly record a bounded snapshot
hermes inbox changes --consumer owner --json
hermes inbox mark-seen --consumer owner --through <observed-sequence>
hermes inbox ack <observed-sequence>
hermes inbox snooze <observed-sequence> --seconds 60
hermes inbox --hide-acked     explicit filter: leave out acknowledged items
hermes inbox --hide-snoozed   explicit filter: leave out currently snoozed items
```

The default snapshot includes processes, native asynchronous delegations, cron,
open chat surfaces and the shared Kanban board. `--fleet` adds local Herald
records; it does not create dispatches, contact remote peers, poll automatically
or grant permission to act. There are no `--herald-fleet` or `--label` flags.

## Herald configuration and coverage

The adapter reads `hermes_herald.origin_name` and `hermes_herald.ledger_file` from
each supplied profile home's `config.yaml`. A falsy ledger path uses that home's
`hermes-herald.db`. Tilde expands normally; `$HERMES_HOME` and `${HERMES_HOME}`
expand to the **supplied profile home**, not another ambient profile. Unresolved
other variables fail closed rather than querying a credential store. Relative
paths retain Herald's current-working-directory semantics.

`config.yaml` is read through Hermes core's `hermes_yaml` module (safe YAML 1.1,
duplicate keys rejected); Inbox declares no YAML dependency of its own.

An explicit nonblank origin name takes precedence over the native profile ID.
Unknown identities cannot trigger unrestricted ledger reads. Fleet discovery is
limited to the native default home and identifiable same-host profiles; arbitrary
supplied directories do not cause discovery of the real user's profiles. At most
33 profile homes are considered, with explicit truncation disclosure.

`herald_meta` reports source availability, observation coverage and truncation.
Each source and the combined result are bounded (20 edges by default, maximum
500 for direct adapter calls). Shared-ledger observations are deduplicated and
conflicting observations are marked. Unreadable origins and other hosts remain
unknown; an empty local ledger does not prove the absence of incoming work.

Only safe identity/status/linkage metadata is collected. Full prompts,
instructions, reasoning and result previews are excluded. Work completion does
not imply an accepted outcome, independently verified evidence or user delivery.
Herald delivery modes (`callback`, `none`, `sync`) are not delivery receipts.

## Source boundary

JSON stores are read as files and SQLite is opened with `mode=ro`; Inbox never
repairs, prunes, migrates or rewrites native task stores. SQLite itself may create
WAL/SHM reader sidecars. Missing or unreadable sources are disclosed in text and
JSON, not treated as verified all-clear. Kanban remains a shared upstream store.
In-memory approvals and clarify prompts have no durable cross-process source;
answer them in the conversation that asked.

## Local observations and attention

Explicit `observe`, `mark-seen`, `ack` and `snooze` actions write only
`<profile-home>/inbox/state.db`. Viewing and `changes` create no database and
advance no cursor. Repeated identical snapshots create no duplicate events;
an observed state change followed by a return to an old state is a new occurrence.
Use the `seq` returned by `changes` for acknowledgments and `--through`.
Without `--through`, `mark-seen` explicitly marks all currently observed events
seen for that consumer; it is not limited to the first returned page.

Acknowledgment and snooze affect one occurrence only, never source execution,
outcome or delivery. They never hide work by default: every view still lists
each waiting item, and acknowledged or snoozed ones are marked `local=acked` or
`local=snoozed` (the `local_state` field in `--json`), in the same key=value form
as work-thread lines. Only the explicit `--hide-acked` and `--hide-snoozed`
filters leave them out of the rendered view and `visible_attention`; the view
then states how many items the filter hid. The raw `attention` list is never
filtered. A thread is marked only when every one of its edges is acknowledged
or snoozed. Expiry is checked on the next view; no scheduler is started.
Unknown occurrences and non-finite or excessive snooze durations are rejected.
The snooze maximum is seven days. Each consumer and profile has independent state.

Five lanes classify explicit known facts: Needs owner, Agent can act, Waiting,
Done and Unknown. `Agent can act` grants no execution permission. `Done` requires
an explicit accepted outcome, not merely a successful exit or completed dispatch.
Current source readers may not provide approval/action/acceptance facts; these
remain unknown. Source conflicts also remain unknown. Evidence references are
reported as unverified; next steps appear only if explicitly supplied.

Native delegation observations are capped at 20 with overflow disclosed; recent
delivered completions are included within the seven-day completion window. This
is not an unlimited task-history export. Unlinked native items remain distinct
from Herald threads: similar names never establish a relationship.

Proposed automatic refresh/result-retrieval hooks remain unactivated. These are
recorded snapshots, not live task heartbeats. The native CLI reads current stores
each time it runs; it does not continuously monitor or infer remote-host state.

## Development

Tests run against Hermes core and isolated temporary stores, using native writer
fixtures for its databases. The compatibility workflow validates the manifest and
runs the Inbox suite on Linux, macOS and Windows. A passing source suite does not
establish installed-artifact or fleet acceptance.
