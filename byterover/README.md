<p align="center"><img src="../assets/byterover.jpg" alt="ByteRover, a Hermes Labs plugin" width="100%"></p>

# ByteRover Memory Provider

> **Labs plugin: works, but unsupported.** ByteRover is now a Hermes Labs plugin: built by Nous Research, not supported, and likely not maintained going forward. Fixes from the community are welcome. See [what "labs" means](../README.md#what-labs-means).

Part of [Hermes Labs Plugins](../README.md): built by Nous Research, **not supported**. Formerly built into
[hermes-agent](https://github.com/NousResearch/hermes-agent) core (`plugins/memory/byterover`); this directory carries its full history.
Profiles that had `memory.provider: byterover` get this plugin installed automatically by `hermes update` or the next agent start.

## Install

```bash
hermes plugins install byterover
hermes config set memory.provider byterover
```

Persistent memory via the `brv` CLI — hierarchical knowledge tree with tiered retrieval (fuzzy text → LLM-driven search).

## Requirements

Install the ByteRover CLI:
```bash
npm install -g byterover-cli
```

(or the install script documented at [byterover.dev](https://byterover.dev)).

## Setup

```bash
hermes memory setup    # select "byterover"
```

Or manually:
```bash
hermes config set memory.provider byterover
# Optional cloud sync:
echo "BRV_API_KEY=your-key" >> ~/.hermes/.env
```

## Config

| Env Var | Required | Description |
|---------|----------|-------------|
| `BRV_API_KEY` | No | Cloud sync key (optional, local-first by default) |

Working directory: `$HERMES_HOME/byterover/` (profile-scoped).

## Tools

| Tool | Description |
|------|-------------|
| `brv_query` | Search the knowledge tree |
| `brv_curate` | Store facts, decisions, patterns |
| `brv_status` | CLI version, tree stats, sync state |
