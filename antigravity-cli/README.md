# Antigravity CLI (`agy`) model provider

Run Hermes on your Google Antigravity subscription through the Antigravity CLI, with no API key
and no HTTP gateway in between: `agy acp` is an [Agent Client Protocol](https://agentclientprotocol.com)
stdio server, and this plugin is the `external_process` provider that drives it.

## Requirements

- The Antigravity CLI on `PATH`, in a release that ships the `acp` subcommand (`agy acp --help`):

  ```
  agy --version          # 1.5.x or newer
  agy acp --help         # prints "Start the Agent Client Protocol (ACP) stdio server"
  ```

- Signed in once, so the CLI holds the credential in the OS keyring:

  ```
  agy                    # interactive; sign in with your Google account
  agy models             # lists the families your account can use
  ```

## Install

```
hermes plugins install NousResearch/hermes-labs-plugins/antigravity-cli
```

Or copy this directory into `~/.hermes/plugins/model-providers/antigravity-cli/` — provider
discovery picks it up on the next provider lookup, no restart needed.

## Use

```
hermes auth status agy                       # row + resolved binary
hermes -m gemini-3.8-flash --provider agy -z "summarize this repo"
```

`/model` and `hermes model` list the provider as **Antigravity CLI (agy)** with the ids the live
session advertises:

| id | family |
|---|---|
| `gemini-3.8-flash` | Gemini 3.8 Flash (the thinking tier is a session config option) |
| `claude-sonnet-4-6` | Claude Sonnet 4.6 |
| `claude-opus-4-6-thinking` | Claude Opus 4.6 (thinking) |
| `gpt-oss-120b-medium` | GPT-OSS 120B |

Short aliases resolve before fuzzy matching: `flash`, `sonnet`, `opus`, `gpt-oss`.

Anything not advertised by `session/new` degrades to the session default with one warning line
instead of failing the turn.

## Launch overrides

The binary and argv can be redirected without editing the plugin (these are executable authority,
so set them in `.env` or the shell — Hermes' generic env writers refuse them):

| Variable | Effect |
|---|---|
| `HERMES_AGY_COMMAND`, `AGY_COMMAND` | binary path, checked in that order |
| `HERMES_AGY_ARGS` | argv tail, shlex-split (default `acp`) |

## How it maps onto Hermes

- `auth_type="external_process"` → credential resolution reports the resolved binary as the
  sign-in evidence; there is no token to pool or refresh.
- `create_client` returns `AgyACPClient`, a thin `agent.acp_client.ACPClient` subclass — the
  public base for ACP providers, the same one the `github-copilot` labs plugin uses.
- Hermes' tool schemas travel into the prompt as text and tool calls are parsed back out of the
  response, so Hermes' tools (terminal, files, web, …) and the CLI's own tools both work.
- Each request is one short-lived `agy acp` session; nothing stays resident between turns.

## Notes and limits

- Text and tool calls are what this path is tested with. Image attachments in the ACP prompt are
  the CLI's business, not the provider's.
- A hung CLI is bounded by the request timeout; the ACP session is terminated with it.
- Antigravity usage limits are the CLI account's, not Hermes': `agy` reports quota errors as
  ordinary model errors.
