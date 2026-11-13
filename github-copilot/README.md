# GitHub Copilot

> **Labs plugin: works, but unsupported.** Nous Research built it and does not maintain it. See
> [what "labs" means](../README.md#what-labs-means).

Part of [Hermes Labs Plugins](../README.md): built by Nous Research, **not supported**. Formerly
bundled in hermes-agent core.

Use your GitHub Copilot subscription as a Hermes model provider. One plugin, two providers:

| Provider | What it talks to | Sign-in |
|---|---|---|
| `copilot` (aliases `github-copilot`, `github-models`, `github`) | The Copilot API (`api.githubcopilot.com`, business/enterprise `api.<plan>.githubcopilot.com`, GitHub Models at `models.github.ai`) with your GitHub account | `hermes auth add copilot`, or a GitHub token in the environment |
| `copilot-acp` (aliases `github-copilot-acp`, `copilot-acp-agent`) | Your installed [GitHub Copilot CLI](https://github.com/github/copilot-cli), driven over ACP (`copilot --acp --stdio`) | The CLI's own `copilot login` |

## Install

```bash
hermes plugins install github-copilot
```

Needs a Hermes release in which Copilot has left core
([hermes-agent#144788](https://github.com/NousResearch/hermes-agent/pull/144788)). On an older Hermes the
plugin stays inactive and logs one warning: that Hermes still ships its own Copilot support, which keeps
working.

## Sign in

**`copilot`:**

```bash
hermes auth add copilot          # GitHub device-code sign-in: open the URL, enter the code
hermes auth add copilot --api-key github_pat_...   # or paste a token
```

The device-code login uses the same GitHub OAuth app as VS Code's Copilot (it mints `ghu_` tokens the
Copilot API exchanges for a short-lived Copilot token, so enterprise-only models work). `hermes model` →
GitHub Copilot offers the same login, and the Desktop app's Accounts tab signs you in without a terminal.

Or set a token. The first supported one wins, in the Copilot CLI's order:

| Env var | Notes |
|---|---|
| `COPILOT_GITHUB_TOKEN` | Where the device-code login in `hermes model` / Desktop saves its token |
| `GH_TOKEN` | |
| `GITHUB_TOKEN` | |

Supported token types: OAuth `gho_*`, GitHub App `ghu_*`, fine-grained PAT `github_pat_*` with the
Copilot Requests permission. Classic PATs (`ghp_*`) are rejected by the Copilot API.
`COPILOT_API_BASE_URL` overrides the API base URL. Requests pin the Copilot API version
(`X-GitHub-Api-Version: 2026-08-01`) so `/models` reports the account's real long-context limits
instead of legacy defaults; override the date with `HERMES_COPILOT_API_VERSION` if GitHub retires
or advances it. The value must be a `YYYY-MM-DD` date: a malformed one is not silently ignored —
every request fails loudly with `400 invalid apiVersion`, so check the value you set first when
requests start failing.

`GH_TOKEN` and `GITHUB_TOKEN` are shared with other GitHub tools, so on their own they never make Hermes
contact GitHub. The plugin calls GitHub (token exchange, model list) only once you have chosen Copilot:
`COPILOT_GITHUB_TOKEN` is set, `copilot` appears in `config.yaml` (`model`, `fallback_providers` /
`fallback_model`, `delegation`, an `auxiliary` task, a MoA slot), you signed in with `hermes auth add
copilot`, or you are in `hermes model` → GitHub Copilot. Until then the model picker shows the curated
Copilot list.

**`copilot-acp`:** install the CLI (`npm install -g @github/copilot`) and run `copilot login`. Hermes
starts `copilot --acp --stdio` per request; point it elsewhere with `HERMES_COPILOT_ACP_COMMAND` (or
`COPILOT_CLI_PATH`) and `HERMES_COPILOT_ACP_ARGS`.

## Premium requests

Copilot reads the `x-initiator` header to tell a request you started (`user`) from an agent follow-up
(`agent`). By design, `copilot` sends `x-initiator: agent` on every request, except the first request of a
user turn on the exact host `api.githubcopilot.com` (and GitHub Models, `models.github.ai`), which says
`user`, as Hermes core did. Accounts whose token exchange hands out a plan host (`api.individual.`,
`api.business.` or `api.enterprise.githubcopilot.com`) send `agent` on every request.

## What changed from the core version: no gh or Copilot CLI token files

Hermes core's Copilot also picked up a token from `gh auth token` and from the gh / Copilot CLI token
files (`~/.config/gh/hosts.yml`, `~/.copilot/config.json`, `~/.config/github-copilot/hosts.json` and
`apps.json`). This plugin does not. Catalog plugins get credentials only through Hermes (catalog rule 11):
reading another tool's login hands its token to code that tool never authorized. If you relied on
`gh auth login`, run `hermes auth add copilot` once, or export `GH_TOKEN="$(gh auth token)"` yourself.
A `gh_cli` credential the old core had pooled is dropped automatically.

`copilot-acp` is unaffected in practice: the Copilot CLI still uses its own login. Hermes only stopped
peeking at the CLI's files to show "signed in"; without an env token the Accounts card reads "Managed by
the GitHub Copilot CLI".

## Disclosure

- `copilot` talks to `github.com` (device-code login), `api.github.com` (`/copilot_internal/v2/token`,
  the GitHub → Copilot token exchange) and `api.githubcopilot.com` / your plan's Copilot host /
  `models.github.ai` (model list and inference). Requests to the `githubcopilot.com` hosts carry
  VS Code-style editor headers (`Editor-Version`, `Copilot-Integration-Id: vscode-chat`) and the
  Copilot API version header (`X-GitHub-Api-Version`), as Copilot requires.
- Tokens are stored by Hermes: the pool in `$HERMES_HOME/auth.json` (`hermes auth add`), env tokens in
  `$HERMES_HOME/.env`, and the exchanged Copilot token (~30 min lifetime) in
  `$HERMES_HOME/.copilot_jwt.json` (mode 0600) so a restart during a network blip can reuse it.
- `copilot-acp` runs your own `copilot` CLI as a subprocess. Whatever that CLI does under its own login
  and settings is between you and GitHub; Hermes sends it the conversation and reads its replies.
- No telemetry.

## Tests

```bash
cd github-copilot
HERMES_AGENT_REPO=../../hermes-agent PYTHONPATH=../../hermes-agent python -m pytest -q
```

On a core that still bundles Copilot the suite skips everything but the inert-guard test.
