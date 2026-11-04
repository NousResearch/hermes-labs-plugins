---
name: patsnap-search
description: "Use when searching patents or scientific papers (prior art, assignees, legal status) through the Patsnap MCP server."
version: 0.1.0
author: Nous Research
license: MIT
metadata:
  tags: "patents, prior-art, literature, patsnap, research, mcp"
  related_skills: "arxiv"
---

# Patsnap patent and literature search

Search Patsnap's global patent database (WIPO/PCT, EPO, USPTO, CNIPA, JPO, KIPO and more) and its
scientific-literature database (PubMed, Nature, arXiv and others) through Patsnap's hosted MCP
server. Patents and papers are separate tools: one search call returns one record type.

## When to Use

- Prior-art or landscape searches around a technical problem, product or R&D topic.
- Patents filtered by assignee, inventor, jurisdiction, IPC, date or legal status.
- Papers filtered by author, institution, date or citation count.
- One known record's details: a patent by publication number, its legal lifecycle, or a paper.
- Resolving a company name to Patsnap's standard assignee, or a company's corporate tree.

Not for statistics, trends or Top-N counts, and never a substitute for a legal opinion or an FTO
conclusion: say so when the user asks for one.

## Prerequisites

- The plugin is enabled and `PATSNAP_API_KEY` is set in the profile's `.env` (Desktop: Settings →
  Plugins → Patsnap). Keys come from https://open.patsnap.com; new accounts get free credits, and
  every call spends credits on the user's Patsnap account.
- If no Patsnap tools are in this session, the key was missing when the session started. Tell the
  user to add it and start a new chat (or run `/reload-mcp`). Do not call the HTTP endpoint yourself
  with curl or a script.

## How to Run

The server registers under the name `patsnap`. Find the exact callable names with `tool_search`
(query `patsnap`) and load schemas with `tool_describe` before the first call; never guess a
prefixed name. The operations below are the server's own tool names.

## Quick Reference

| Operation | Use |
|---|---|
| `search_patent` | Patent search: `search_strategy` of `semantic` (needs `semantic_query`), `keyword` (needs `keywords`), `filter` (needs `filters` or `domain_filters`), or `boolean` alone (needs `boolean_query`) |
| `search_literature` | Paper search: `semantic`, `keyword` or `filter` (no `boolean`, no domains) |
| `build_patent_boolean_query` | Turn a full natural-language patent intent into a Patsnap Boolean query; pass `data.boolean_query` unchanged to `search_patent` with `search_strategy=["boolean"]` |
| `describe_search_capability` | Fields and operators for the `ls` (life sciences) or `material` patent domain; call before using a non-general domain |
| `get_standard_assignee_name` | Resolve a company or applicant name to `company_id` / `ans_id` candidates |
| `get_corptree_hierarchy` | Ancestors, children, descendants or root group of a confirmed `company_id` |
| `fetch_patent` | One patent by internal id or publication number (bibliography, claims, description by default) |
| `fetch_patent_lifecycle` | Legal events, annuities, transfers, licenses, litigation for one patent |
| `fetch_literature` | One paper by internal `paper_id` (not a URL): bibliography and abstract |

Search pagination: `limit` 1–100, `offset >= 0`, `offset + limit <= 1000`. Detail tools take
`language` (`en`, `zh-CN`, `zh-TW`, `ja`) and `format` (`json` or `markdown`).

## Procedure

1. Decide patents, papers or both; both means two searches.
2. Pull concepts, keywords, assignees, classifications, dates and jurisdictions out of the request.
   An assignee given as a short name, alias or translation: resolve it with
   `get_standard_assignee_name` first and filter on the returned id.
3. Search with only the parameters the chosen strategy needs (strategies and parameters are
   strictly bound; omit a strategy's parameter when that strategy is not used). Start with a small
   `limit` (10–20) to keep credit use down; page only when the user needs more.
4. Fetch only the records the answer needs, one per call.
5. Answer with evidence: titles, publication numbers or paper ids, and the record URLs returned.

## Pitfalls

- `company_id` and `ans_id` are not interchangeable. A parent or subsidiary found with
  `get_corptree_hierarchy` does not widen a patent search unless you add its ids.
- `fetch_patent` already includes full-text sections; `include` accepts only `relations` and `media`,
  which cost extra calls and time. Lifecycle data needs `fetch_patent_lifecycle`.
- Do not invent domain fields, operators or identifiers; take them from
  `describe_search_capability` and earlier results.
- An authentication error ("Access token expired or authentication error") means the key is wrong,
  expired or out of credits. Report it and stop; do not retry in a loop.

## Verification

A working setup returns ranked results with publication numbers or paper ids for a query such as
"solid-state battery electrolyte interface stability, last five years, newest first".
