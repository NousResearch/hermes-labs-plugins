<p align="center"><img src="../assets/dingtalk.jpg" alt="DingTalk, a Hermes Labs plugin" width="100%"></p>

# DingTalk

> **Labs plugin: works, but unsupported.** DingTalk is now a Hermes Labs plugin: built by Nous Research, not supported, and likely not maintained going forward. Fixes from the community are welcome. See [what "labs" means](../README.md#what-labs-means).

Part of [Hermes Labs Plugins](../README.md): built by Nous Research, **not supported**. Formerly built into
[hermes-agent](https://github.com/NousResearch/hermes-agent) core (`plugins/platforms/dingtalk` and
`hermes_cli/dingtalk_auth.py`); this directory carries their full history.

Connect Hermes to [DingTalk](https://www.dingtalk.com/) (钉钉) as a chatbot, in direct messages and group
chats. The adapter uses DingTalk's Stream Mode, a long-lived WebSocket opened from your machine (no public
URL or webhook server), and replies with markdown through each message's session webhook, or with AI Cards.

Everything an existing setup relies on is unchanged: platform name `dingtalk`, every `DINGTALK_*` env var,
the `platforms.dingtalk` (and `gateway.platforms.dingtalk`) config keys and `DINGTALK_HOME_CHANNEL`.

## Install

```bash
hermes plugins install dingtalk
```

The plugin brings its own dependencies (`dingtalk-stream`, `alibabacloud-dingtalk`, `qrcode`, and the
`websockets` version `dingtalk-stream` needs). If the gateway reports `dingtalk-stream not installed` or a
websocket incompatibility, reinstall the plugin (`hermes plugins update dingtalk`).

Needs a Hermes release in which DingTalk has left core. On an older Hermes the plugin stays inactive and
logs one line (`the plugin stays inactive until core drops it`): that Hermes still ships its own DingTalk
support, which keeps working. The check is whether core's `plugins/platforms/dingtalk/` is present, not
whether the built-in is enabled.

## How Hermes behaves

| Context | Behavior |
|---|---|
| **DMs (1:1 chat)** | Hermes answers every message; each DM has its own session. |
| **Group chats** | With `require_mention: true`, Hermes answers only when @mentioned (or a `mention_patterns` regex matches, or the chat is in `free_response_chats`). |
| **Shared groups** | By default each user gets their own session inside the group (`group_sessions_per_user: true` in `config.yaml`). |

## Setup

### 1. Create a DingTalk app (or use the QR flow in step 3)

1. Go to the [DingTalk Developer Console](https://open-dev.dingtalk.com/) and log in with an admin account.
2. **Application Development** → **Custom Apps** → create an app (H5 Micro-App or **Robot**, depending on
   your console version).
3. Under **Credentials & Basic Info**, copy the **Client ID** (AppKey) and **Client Secret** (AppSecret).
   The secret is shown once.
4. **Add Capability** → **Robot**, enable it, and choose **Stream Mode** for message reception.

### 2. Find your DingTalk user ID

User IDs are set by your organization's admin (**Contacts** → **Members** in the admin console). The bot
also logs each sender's `sender_id`: start the gateway, message the bot, and read it from
`hermes logs gateway`.

### 3. Run the setup wizard

```bash
hermes gateway setup
```

Select **DingTalk**, then either:

- **QR code (recommended):** scan the QR printed in your terminal with the DingTalk mobile app. The Client
  ID and Client Secret come back automatically and are saved to your profile's `.env`. Without the `qrcode`
  package the wizard prints the authorization link instead. The consent page is branded `openClaw`
  (DingTalk's onboarding bridge for this flow); the bot you create is yours and private to your tenant.
- **Manual input:** paste the Client ID and Client Secret.

### 4. Environment variables

In `~/.hermes/.env` (or your profile's `.env`):

```bash
# Required
DINGTALK_CLIENT_ID=your-app-key
DINGTALK_CLIENT_SECRET=your-app-secret

# Who may talk to the bot (comma-separated; * = anyone)
DINGTALK_ALLOWED_USERS=user-id-1,user-id-2
# DINGTALK_ALLOW_ALL_USERS=true          # dev only

# Group-chat gating
# DINGTALK_REQUIRE_MENTION=true
# DINGTALK_MENTION_PATTERNS=["^小马"]      # JSON list or comma-separated regexes
# DINGTALK_FREE_RESPONSE_CHATS=cidABC==,cidDEF==
# DINGTALK_ALLOWED_CHATS=cidABC==         # empty = every chat

# Cron / notification delivery
# DINGTALK_HOME_CHANNEL=cidXXXX==
# DINGTALK_HOME_CHANNEL_NAME=Ops
# DINGTALK_WEBHOOK_URL=https://oapi.dingtalk.com/robot/send?access_token=...
```

`DINGTALK_WEBHOOK_URL` is a static robot webhook used for out-of-process sends (cron jobs, `send_message`
from another platform): session webhooks are only available while replying to an inbound message.

### 5. Config keys

The same settings can live in `config.yaml` under `platforms.dingtalk` (or
`gateway.platforms.dingtalk`); env vars win when both are set:

```yaml
group_sessions_per_user: true

platforms:
  dingtalk:
    enabled: true
    require_mention: true
    mention_patterns: ["^小马"]
    free_response_chats: [cidABC==]
    allowed_chats: [cidABC==, cidDEF==]
    allowed_users: [user-id-1, user-id-2]
    extra:
      client_id: your-app-key          # alternative to DINGTALK_CLIENT_ID
      client_secret: your-app-secret   # alternative to DINGTALK_CLIENT_SECRET
      robot_code: your-robot-code      # defaults to the client id
      card_template_id: your-card-template-id   # enables AI Cards
      webhook_url: https://oapi.dingtalk.com/robot/send?access_token=...
```

`allowed_users` may also sit at `gateway.platforms.dingtalk.extra.allowed_users`; it is bridged to
`DINGTALK_ALLOWED_USERS`, which is what the gateway's authorization check reads.

### 6. Start the gateway

```bash
hermes gateway
```

The bot connects within a few seconds. Message it in a DM, or @mention it in a group it has joined.

## Features

- **Stream Mode**: no public URL; works behind NAT and firewalls. Reconnects with exponential backoff
  (2s → 5s → 10s → 30s → 60s), with a circuit breaker for a repeating error.
- **AI Cards** (`card_template_id`): replies stream into a card instead of plain markdown.
- **Emoji reactions**: 🤔Thinking while working, 🥳Done when finished (localized like core's strings).
- **Inbound media**: images, audio, video, files and rich text are resolved so vision and file tools can
  use them.
- **Deduplication** of redelivered messages; replies are capped at 20,000 characters.

Per-platform display settings work as before:

```yaml
display:
  platforms:
    dingtalk:
      streaming: true
      tool_progress: off
      interim_assistant_messages: false
```

## Troubleshooting

- **Bot not responding**: check the robot capability is on with Stream Mode, and that your user ID is in
  `DINGTALK_ALLOWED_USERS`. Without an allowlist the gateway denies everyone.
- **"DINGTALK_CLIENT_ID and DINGTALK_CLIENT_SECRET required"**: set both in `.env` (or `extra`).
- **"dingtalk-stream cannot open its websocket"**: the installed `websockets` does not match
  `dingtalk-stream` 0.24.3; reinstall the plugin.
- **"No session_webhook available"**: the reply came after the inbound message's webhook expired (or a
  restart); send a new message.

## Disclosure

- Talks to DingTalk's endpoints only: the Stream Mode gateway, the session/robot webhooks
  (`*.dingtalk.com`), the DingTalk OpenAPI (AI Cards, emoji reactions, media download) and, during QR setup,
  the device-flow registration API (`https://oapi.dingtalk.com/app/registration/*`).
- Received media is cached in the profile's Hermes media cache; QR setup writes the credentials to the
  profile's `.env`.
- No telemetry of its own.

## Tests

```bash
cd dingtalk
HERMES_AGENT_REPO=../../hermes-agent PYTHONPATH=../../hermes-agent python -m pytest -q
```

The DingTalk SDKs are faked where tests need them; two tests that exercise the real `dingtalk-stream` and
`qrcode` packages skip when those are absent. On a core that still bundles DingTalk the plugin is inert and
the tests that need the plugin's own registry entry are skipped.

## License

MIT
