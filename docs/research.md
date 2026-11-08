# Research sources (development)

Research is opt-in. Existing AIOS-Client installations have **no** granted research
sources. No Google, paid API, or SearXNG service is required to run the client.

## Configure an existing SearXNG server

Create `research_sources.json` in the **same directory as the AIOS-Client
configuration JSON**, for example:

```json
{
  "version": 1,
  "sources": [
    {
      "id": "home-search",
      "adapter": "searxng",
      "endpoint": "http://127.0.0.1:8080",
      "enabled": true,
      "priority": 10,
      "topics": ["general", "news"]
    }
  ]
}
```

Set the endpoint to the actual SearXNG base URL **reachable from the client
host**. If SearXNG runs on a different LAN machine, localhost is not appropriate.
SearXNG must have `json` enabled under `search.formats` in its settings.

In the relevant agent profile of the normal AIOS-Client configuration, add:

```json
"research_sources": ["home-search"],
"tool_mode": "research"
```

`tool_mode: research` exposes only the three research discovery tools,
result publication, planning, and explicitly scoped AIOS recall/work callbacks.
It does **not** expose shell, host inspection, file editing, or arbitrary HTTP.
Omit `tool_mode` to retain ordinary workspace behavior. An empty grant list
never grants sources implicitly. The research tools are not registered at all
until the agent has a source grant.

The operator owns the source registry. Grants refer to source IDs; model/tool
arguments cannot add endpoints. Unknown and disabled sources fail closed.

## What Pass 1 supports

- `research_sources`: enumerate the sources granted to this agent.
- `research_search`: bounded SearXNG JSON searches, or authorized `local_text`
  source searches. By default, try sources in configured priority order (at
  most two), moving to the next only if no useful results are returned.
- `research_read`: revisit a bounded **search excerpt** by its evidence ID
  in the current work run. It does NOT fetch an entire page or book.

A local text source may be declared with `adapter: "local_text"` and an
absolute `root` folder, and individually granted to an agent. It currently
searches UTF-8 text, Markdown, CSV, JSON and RST, with bounded file sizes and
scan counts. PDF, EPUB, Calibre, OCR, full-text indexing and full-page retrieval
are deliberately deferred to later passes.

Search-result URLs are untrusted references, **never** fetch targets. HTTP
requests go only to configured SearXNG endpoints, without redirects, cookies,
proxy environment variables or automatic credentials. A private LAN endpoint
must be intentionally configured by the operator. Do not configure untrusted
servers or send secret content as search terms.

These research results are discovery evidence, not verified citations. Do not
represent a snippet as a full-page inspection. Existing report and
`memory_candidates` behavior is unchanged; durable structured citation
publication is planned for a subsequent pass.

## Source registry format

Each source has a unique lowercase `id`, `adapter`, optional `enabled`
(default true), `priority` (lower first), and optional `topics`.
SearXNG uses `endpoint`. Local text uses `root`. No secret values belong
in this file. Changes take effect on subsequent work runs.

## Security and compatibility

The existing `workspace.http` probe and read-only inspection policy remain
unchanged. The dedicated `research` tool mode uses an allowlist enforced both
at model catalog and tool execution, independent of objective prose. Legacy
agents default to `workspace` tool mode, preserving prior behavior. Research
service credentials, external page reading, and source-aware publication need
separate security and evidence designs before release.
