# Patsnap

> Part of [Hermes Labs Plugins](../README.md): built by Nous Research, not supported.

Search global patents and scientific literature from Hermes through
[Patsnap](https://www.patsnap.com)'s own hosted MCP server. Patent search covers WIPO/PCT, EPO,
USPTO, CNIPA, JPO, KIPO and many more authorities; literature search covers PubMed, Nature, arXiv
and other journals and repositories. The agent can also resolve a company to its standard patent
assignee, walk its corporate tree, and read one patent's legal lifecycle.

This is a portable [Agent Plugins v1](https://agent-plugins.org) package. One install ships:

- `mcp.json`: Patsnap's hosted Streamable-HTTP MCP server at
  `https://connect.patsnap.com/2b0355/logic-mcp`, connected for every new session while the plugin
  is enabled. No `mcp_servers` edit in config.yaml, nothing to install locally.
- `skills/patsnap-search/`: a workflow skill (when to use which tool, the strict
  strategy/parameter rules, assignee resolution, credit-saving defaults).

## Install

```bash
hermes plugins install patsnap
hermes plugins enable patsnap
```

Then add your Patsnap API key. In Hermes Desktop: **Settings → Plugins → Patsnap**, paste it into
*Patsnap API key*, Save. Without Desktop:

```bash
hermes config set PATSNAP_API_KEY <your key>      # writes the profile's .env
# or put PATSNAP_API_KEY=<your key> in the profile's .env by hand
```

Create the key on the [Patsnap Open Platform](https://open.patsnap.com/marketplace/mcp-servers/patsnap-search);
new accounts get free credits, heavier use is billed by Patsnap. Start a new chat after adding the
key. Then ask, for example, "Find patents from the last five years on solid-state battery
electrolyte-interface stability, newest first".

## How the key is sent

Hermes reads `PATSNAP_API_KEY` from the active profile's `.env` or secret source when it connects
and sends it in an `apikey` request header. It is never put in the URL, so it does not appear in
logs or the connection list, and Hermes drops the header if the server ever redirects to another
origin. If the key is missing, Hermes does not connect and says which variable to set.

## Tools

Patsnap lists these tools for the hosted server: `search_patent`, `search_literature`,
`build_patent_boolean_query`, `describe_search_capability`, `get_standard_assignee_name`,
`get_corptree_hierarchy`, `fetch_patent`, `fetch_patent_lifecycle`, `fetch_literature`. All read
Patsnap data; none writes, sends or buys anything. Every call does spend credits on your Patsnap
account, so the skill keeps searches small and fetches only what the answer needs.

## Disclosure

- Network: queries and record ids go to Patsnap's hosted service (`connect.patsnap.com`) under your
  key, and results come back from it. Nothing else is contacted.
- Credentials: only `PATSNAP_API_KEY`, read through Hermes; the plugin has no code of its own.
- No local processes, files or telemetry.

## Attribution

The MCP server is Patsnap's
([patsnap/patent-literature-search-mcp](https://github.com/patsnap/patent-literature-search-mcp),
Apache-2.0). This package only points Hermes at it; the skill draws on Patsnap's published tool
guide.
