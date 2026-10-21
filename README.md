<h1 align="center">Hermes Labs Plugins ⚗</h1>

<p align="center">
  <b>Experimental plugins for <a href="https://github.com/NousResearch/hermes-agent">Hermes Agent</a> that Nous Research built but does not support.</b><br>
  Each one works and installs from the Hermes plugin catalog, listed in the community tier.
</p>

<p align="center">
  <a href="https://hermes-agent.nousresearch.com/docs/plugins"><img src="https://img.shields.io/badge/Plugin_Catalog-browse-FFDE1A?style=for-the-badge&labelColor=0B0C0E" alt="Plugin Catalog"></a>
  <a href="https://github.com/NousResearch/hermes-labs-plugins/actions/workflows/tests.yml"><img src="https://img.shields.io/github/actions/workflow/status/NousResearch/hermes-labs-plugins/tests.yml?branch=main&style=for-the-badge&label=tests&labelColor=0B0C0E&color=103CE7" alt="Tests"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-E5DEC9?style=for-the-badge&labelColor=0B0C0E" alt="MIT"></a>
</p>

---

## What "labs" means

These plugins started as Hermes features we decided not to put in the core. They are here so anyone who wants them can install them. They are **not supported**:

- **No promise of fixes.** A bug report may sit, and a core change may break a plugin before anyone notices. CI runs every plugin against current Hermes nightly, so breakage is at least visible.
- **Community tier in the catalog.** The catalog shows these as community plugins, not official ones, so nobody installs one thinking Nous maintains it.
- **Up for adoption.** If you use one and want to own it, open an issue. We'll help you move it to your own repo and point the catalog entry there.

Supported first-party plugins live in [hermes-official-plugins](https://github.com/NousResearch/hermes-official-plugins).

## Plugins

| Plugin | Install | What it does |
|---|---|---|
| [Telegram Business Mode](telegram-business/) | `hermes plugins install hermes-telegram-business` | Turns Hermes into an observe-with-approval secretary for your Telegram Business account. It drafts replies to your customers, and nothing is sent until you approve it. |
| [Inbox](inbox/) | `hermes plugins install inbox` | `hermes inbox` shows everything in flight for the active profile in one read-only view: failed cron runs, stalled or dropped delegations, orphaned background processes and blocked kanban tasks, plus running work, undelivered results, upcoming jobs and open chat sessions. |
| [Workflow Record](workflow-record/) | `hermes plugins install workflow-record` | `hermes record` captures a browser workflow you demonstrate once (clicks, field values, navigations; passwords masked inside the page), and its bundled skill lets `/learn` turn the recording into a skill that replays it. |

## Working on a plugin

Same layout and rules as the official repo: each top-level directory is one self-contained plugin with its own `plugin.yaml`, `README.md` and `tests/`. Run a suite against core with:

```bash
cd telegram-business
HERMES_AGENT_REPO=../../hermes-agent PYTHONPATH=../../hermes-agent python -m pytest -q
```

Merging here ships nothing to users. The release is the catalog pin on hermes-agent (`plugin-catalog/<name>.yaml`, `sha:` + `subdir:`).
