# Inbox

> **Labs plugin: works, but unsupported.** Nous Research built it and does not maintain it. See
> [what "labs" means](../README.md#what-labs-means).

`hermes inbox` shows everything Hermes has in flight for the active profile in one read-only view:
what needs your attention, what is running, which background results are waiting, what is
scheduled and which chat surfaces are open. Inspired by Cursor's Inbox.

```
$ hermes inbox

  ◆ Needs attention (4)
    ⚠ cron 'nightly-backup'  last run FAILED: Script exited with code 1 stderr: backup target /mnt/nas not mounted
    ⚠ cron 'feed-watch'  paused (waiting on API key)
    ⚠ process proc_9f2c44  [GONE (stale checkpoint), 3h13m]  pytest -x tests/ --watch
    ⚠ kanban t_12200367  [blocked]  Get Stripe test keys from finance

  ◆ In progress (2)
    ◐ process proc_7c1e02  [running, 22m]  npm run dev -- --port 5173
    ◐ delegation dlg_41a7  [running, 5m ago]  Research venue pricing for 40 people in Austin

  ◆ Finished, result not yet delivered (1)
    ✔ delegation dlg_3b90  [completed, 3m ago]  (result awaiting delivery)  List every caller of charge_customer()

  ◆ Scheduled (2 active job(s))
    ⏱ nightly-backup  next: 2026-10-21T06:08:46.190275-07:00
    ⏱ morning-digest  next: 2026-10-21T08:00:00-07:00

  ◆ Open surfaces (2)
    ◎ session [cli] pid=1197440  Plan the offsite
    ◎ session [tui] pid=1197519  Refactor the billing module
```

## Install

```bash
hermes plugins install inbox
```

It adds one CLI command (`hermes inbox`). No model tools, no hooks, nothing runs during chats. No
dependencies beyond Hermes itself. The agent can still use it: ask it to run `hermes inbox --json`
in the terminal.

## Usage

```bash
hermes inbox            # the view above
hermes inbox --json     # the same snapshot as JSON, for scripts and dashboards
hermes -p work inbox    # another profile's inbox (install the plugin in that profile too)
```

Sections with nothing in them are left out, except "Needs attention" and "In progress", which
always print so an empty inbox reads as all clear.

| Section | What appears |
|---|---|
| Needs attention | Stalled or `unknown` delegations, delegation results dropped after failed deliveries (last 7 days), cron jobs whose last run failed or that were paused with a reason, background processes still in the checkpoint whose PID is gone, kanban tasks that are blocked or waiting for review. |
| In progress | Live background processes, running async delegations, running kanban tasks. |
| Finished, result not yet delivered | Delegations that completed but whose result has not reached its conversation yet. |
| Scheduled | Enabled cron jobs and the next five runs. |
| Open surfaces | Open CLI, TUI, Desktop and gateway chat sessions whose process is still alive, with the session title. |

## What it reads

Everything is read-only: JSON files are read as files, SQLite databases are opened with
`mode=ro`, and session titles come from Hermes' read-only `SessionDB` attach. It never writes,
repairs or prunes a store (SQLite may create a database's `-wal`/`-shm` sidecar files, as it does
for any reader), and works whether or not a gateway is running. Paths resolve against
the active profile's home (`HERMES_HOME`, or `hermes -p <profile>`), except the kanban board,
which Hermes shares across profiles.

| Source | Store | If it is missing or unreadable |
|---|---|---|
| Background processes | `<home>/processes.json` (the process registry checkpoint). Host PIDs are re-checked with `psutil`; PIDs inside a sandbox backend can't be probed and show as `sandboxed`. | Section empty; listed under "Not read". |
| Async delegations | `async_delegations` table in `<home>/state.db` | Same. A locked database gives up after 2 seconds instead of hanging. |
| Cron jobs | `<home>/cron/jobs.json` | Same. |
| Open chat surfaces | `<home>/runtime/active_sessions.json` (active-session leases), titles from `state.db` via `SessionDB(read_only=True)`. A lease counts only while its PID is alive and, when recorded, its process start time still matches (a recycled PID is not counted). | Same; without `state.db` the session id shows instead of the title. |
| Kanban | The current board's `kanban.db`, resolved the documented way: `HERMES_KANBAN_DB`, else `HERMES_KANBAN_BOARD`, else `<root>/kanban/current`, else `<root>/kanban.db`, with `<root>` = `HERMES_KANBAN_HOME` or the default Hermes root. | Same. |

Every section that could not be read is named at the bottom of the text view ("Not read") and in
the `sources` field of the JSON (`"read"`, `"absent"` or `"unreadable (...)"`).

**Not shown:** pending approvals and clarify prompts. They live in the memory of the process that
asked (the gateway or an open CLI), with no store another process can read, so answer them in
that chat.

## Configuration

None.

## Development

```bash
cd inbox
HERMES_AGENT_REPO=../../hermes-agent PYTHONPATH=../../hermes-agent python -m pytest -q
```

The tests seed a temp `HERMES_HOME` through Hermes' own writers (`SessionDB` for `state.db`,
`kanban_db.init_db` for `kanban.db`), so the queries run against core's real schema, and they
check that building the inbox writes nothing to the home it reads.

Origin: hermes-agent PR #81507 (`hermes inbox`), moved out of core into this plugin.
