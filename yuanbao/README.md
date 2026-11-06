<p align="center"><img src="../assets/yuanbao.jpg" alt="Yuanbao, a Hermes Labs plugin" width="100%"></p>

# Yuanbao

> **Labs plugin: works, but unsupported.** Yuanbao is now a Hermes Labs plugin: built by Nous Research, not supported, and likely not maintained going forward. Fixes from the community are welcome. See [what "labs" means](../README.md#what-labs-means).

Part of [Hermes Labs Plugins](../README.md): built by Nous Research, **not supported**. Formerly built into
[hermes-agent](https://github.com/NousResearch/hermes-agent) core (`plugins/platforms/yuanbao` and
`tools/yuanbao_tools.py`); this directory carries their full history.

Connect Hermes to [Yuanbao](https://yuanbao.tencent.com/), Tencent's messaging platform. The adapter keeps a
persistent WebSocket connection (HMAC-signed sign token) and supports direct (C2C) and group conversations,
plus the `yuanbao` toolset: `yb_query_group_info`, `yb_query_group_members`, `yb_send_dm`,
`yb_search_sticker` and `yb_send_sticker`.

Everything an existing setup relies on is unchanged: platform name `yuanbao`, toolset `yuanbao`, the yb_*
tool names, every `YUANBAO_*` env var, `platforms.yuanbao` (and the legacy `gateway.yuanbao`) config keys and
`YUANBAO_HOME_CHANNEL`. Hermes installs this plugin automatically for profiles that used the built-in
Yuanbao platform.

## Install

```bash
hermes plugins install yuanbao
```

Needs a Hermes release in which Yuanbao has left core. On an older Hermes the plugin stays inactive and
logs one line (`the plugin stays inactive until core drops it`): that Hermes still ships its own Yuanbao
support, which keeps working.

The check is whether core's Yuanbao files are present (`plugins/platforms/yuanbao/` or
`tools/yuanbao_tools.py`), not whether core's built-in Yuanbao is enabled. On such a Hermes the plugin stays
inactive even if you disabled the built-in (`plugins.disabled: [yuanbao-platform]`), so neither runs:
re-enable the built-in there, or update Hermes.

## Setup

### 1. Create a bot in Yuanbao

1. Download the Yuanbao app from [https://yuanbao.tencent.com/](https://yuanbao.tencent.com/)
2. In the app, go to **PAI → My Bot** and create a new bot
3. After the bot is created, copy the **APP_ID** and **APP_SECRET**

### 2. Run the setup wizard

```bash
hermes gateway setup
```

Select **Yuanbao**. The wizard asks for your App ID and App Secret and saves them. The WebSocket URL and
API domain have built-in defaults; you only need the App ID and App Secret to get started.

### 3. Environment variables

Verify these in `~/.hermes/.env` (or your profile's `.env`):

```bash
# Required
YUANBAO_APP_ID=your-app-id          # legacy name YUANBAO_APP_KEY still works
YUANBAO_APP_SECRET=your-app-secret

# Optional overrides (defaults: wss://bot-wss.yuanbao.tencent.com/wss/connection, https://bot.yuanbao.tencent.com)
# YUANBAO_WS_URL=wss://...
# YUANBAO_API_DOMAIN=https://...

# Optional: bot account ID (normally obtained automatically from the sign token)
# YUANBAO_BOT_ID=your-bot-id

# Optional: internal routing environment (e.g. test/staging/production)
# YUANBAO_ROUTE_ENV=production

# Optional: home channel for cron/notifications (format: direct:<account> or group:<group_code>)
YUANBAO_HOME_CHANNEL=direct:bot_account_id
YUANBAO_HOME_CHANNEL_NAME="Bot Notifications"

# Optional: restrict access (legacy, see Access control below for fine-grained policies)
YUANBAO_ALLOWED_USERS=user_account_1,user_account_2
```

### 4. Start the gateway

```bash
hermes gateway
```

The adapter connects to the Yuanbao WebSocket gateway, authenticates with HMAC signatures and starts
processing messages.

## Features

- **WebSocket gateway** with heartbeat/keep-alive and automatic reconnection (exponential backoff)
- **C2C and group messaging**, with message deduplication
- **Media**: images, files and voice via COS (Tencent Cloud Object Storage); media URLs are SSRF-checked
  before download
- **Markdown-aware chunking** for Yuanbao's message size limit
- **Stickers**: native TIMFaceElem stickers through `yb_search_sticker` / `yb_send_sticker`
- **Group queries**: group details and member lists (`yb_query_group_info` / `yb_query_group_members`)
- **DMs from a group**: `yb_send_dm` looks a member up by name and messages them privately
- **Skill**: `yuanbao:yuanbao` (formerly core's optional `yuanbao` skill) walks the model through
  @mentions, DMs and member lookups with these tools
- **WeChat forwarded chat history**: forwarded bundles (including nested forwards) are decoded and injected
  into the conversation
- **Auto-sethome**: with no home channel set, the first eligible sender becomes the home channel
- **Slow-response notice** while the agent works on a long task

## Chat ID formats

| Chat type | Format | Example |
|---|---|---|
| Direct message (C2C) | `direct:<account>` | `direct:user123` |
| Group message | `group:<group_code>` | `group:grp456` |

`hermes send yuanbao:group:<code>` and `send_message` accept the same forms (a bare number is a group code).

## Home channel

Use `/sethome` in any Yuanbao chat to make it the home channel; cron jobs deliver there. Or set
`YUANBAO_HOME_CHANNEL` (and optionally `YUANBAO_HOME_CHANNEL_NAME`) in `.env`.

## Access control

```bash
# DM policy: open (default) | allowlist | disabled
YUANBAO_DM_POLICY=open
# Comma-separated user IDs allowed to DM the bot (only used when DM_POLICY=allowlist)
YUANBAO_DM_ALLOW_FROM=user_id_1,user_id_2

# Group policy: open (default) | allowlist | disabled
YUANBAO_GROUP_POLICY=open
# Comma-separated group codes allowed (only used when GROUP_POLICY=allowlist)
YUANBAO_GROUP_ALLOW_FROM=group_code_1,group_code_2
```

Or in `config.yaml`:

```yaml
platforms:
  yuanbao:
    extra:
      dm_policy: allowlist
      dm_allow_from: "user1,user2"
      group_policy: open
      group_allow_from: ""
```

## Troubleshooting

- **Bot online but not answering**: the WebSocket handshake failed to authenticate. Check App ID and App
  Secret, that the bot has permissions, and `hermes logs gateway`.
- **"Connection refused"**: the WebSocket URL is unreachable or wrong (it must start with `wss://`); check
  network access to the API domain.
- **Media uploads fail**: check `YUANBAO_API_DOMAIN` and that media upload is enabled for your bot.
- **Frequent disconnects**: run `hermes gateway run -vv` for verbose logs and look for the error pattern
  (older Chinese docs said `HERMES_LOG_LEVEL=debug`; Hermes does not read that variable).

## Disclosure

- The adapter talks to Tencent's Yuanbao bot endpoints: the sign-token API (`YUANBAO_API_DOMAIN`, default
  `https://bot.yuanbao.tencent.com`), the bot WebSocket (`YUANBAO_WS_URL`, default
  `wss://bot-wss.yuanbao.tencent.com/wss/connection`) and Tencent COS for media upload/download. Sign-token
  requests carry the Hermes version and an instance id header.
- Received media is cached in the Hermes media cache of the profile; the home channel is written to the
  profile's `.env` / `config.yaml` when set by `/sethome` or auto-sethome.
- No telemetry of its own.

## Tests

```bash
cd yuanbao
HERMES_AGENT_REPO=../../hermes-agent PYTHONPATH=../../hermes-agent python -m pytest -q
```

On a core that still bundles Yuanbao the plugin is inert, and the tests that need the plugin's own
registry entry are skipped.

## License

MIT
