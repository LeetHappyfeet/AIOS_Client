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

## Pass 2: authorized document reading

New adapters: `local_documents` and `calibre_opds`. Local documents support
UTF-8 text, Markdown, HTML, CSV, JSON, RST, text-layer PDFs and EPUB files.
Text extraction is bounded by file size and page/section count; scanned PDFs
without OCR text cannot be searched. The default implementation scans files on
demand; **incremental full-text indexing is not yet implemented**.

Example local collection:

```json
{"id":"manuals","adapter":"local_documents","root":"/srv/manuals",
 "priority":5,"topics":["technical"]}
```

Example Calibre Content Server:

```json
{"id":"library","adapter":"calibre_opds",
 "endpoint":"http://calibre.lan:8080/opds","priority":10}
```

Calibre uses OPDS search, first with `full_text=y` and then metadata search if
full-text results are unavailable. Enable Calibre's full-text indexing for
content-based discoveries. This first adapter
requires an accessible, anonymous OPDS catalog; authentication is deliberately
not attempted. When an OPDS book advertises an EPUB or PDF acquisition link on
the **same origin**, `research_read` downloads a bounded copy and extracts its
first textual section. No arbitrary links are followed.

SearXNG result pages remain blocked by default. To approve reading pages at
specific websites, put exact origins in the SearXNG source:

```json
"read_origins": ["https://www.example.org"]
```

An approved result URL can then be passed indirectly through its `evidence_id`
to `research_read`. URL strings supplied directly by models cannot cause page
fetches. Redirects and environment proxy credentials remain disabled.
Allowlisted DNS hosts are a trust choice by the operator; do not permit unknown
or sensitive local destinations. There is no general-purpose browser.

`research_read` returns bounded text and a `next_cursor`; local document
extracts are tied to a SHA-256 snapshot captured at search time. Evidence
is **run-scoped**, not an AIOS memory fact, and must be attributed and checked
before making durable factual claims. These new tools do not automatically
write books or raw retrievals into character memory.

The next iteration should add incremental SQLite FTS5 indexing, a complete
Calibre section navigator, credentials, and richer citation publishing.

## Optional local full-text index

For large `local_documents` sources, set `"indexed": true`. AIOS-Client uses a
per-agent SQLite FTS5 cache (`client-data/agents/<id>/research-index.sqlite3`).
Changes to files are refreshed on the next search; the first scan reads up to
1,000 eligible files. This cache is not character memory and should remain
private to the operating-system account. No extra server is required.

## Pass 3: evidence receipts, citations and controlled memory nominations

Search snippets remain discovery data only. `research_read` establishes a
bounded, run-scoped **retrieval receipt** (not independent factual verification).
`research_evidence(evidence_id)` returns receipt metadata without full text.
Receipts are kept in a per-agent SQLite cache at
`client-data/agents/<id>/research-evidence.sqlite3`. The store retains
bounded excerpts, source and section identifiers, hashes and retrieval times;
it does **not** copy whole documents into AIOS or the character HUD. Up to
5,000 recent receipts are retained per agent.

To associate a claim with read evidence, use `publish_result` with the
optional `research_citations` argument:

```json
{
  "outcome": "completed",
  "summary": "Compared the retrieved sources",
  "findings": ["A documented observation with qualified scope"],
  "research_citations": [
    {"finding_index": 0, "evidence_id": "24-character hex receipt id", "stance": "supports"}
  ]
}
```

A citation to an unread snippet, unknown ID, or another run is rejected.
`stance` may be `supports` or `contradicts` so conflicting evidence can be
retained without forcing false certainty. Citations are included in the
bounded `result_summary` sent to AIOS, without raw pages.

When AIOS advertises compact workspace memory, explicit `finding`
memory candidates in research mode must link to a supported citation using
`evidence_id` and `finding_index`. The client validates those references and
sends only the **existing** memory-candidate shape expected by AIOS. Private
library references are not converted into public URLs. Unlinked ideas and
attempts retain their earlier nomination behavior, but no research content
is automatically inserted into character memory.

No AIOS server schema or RDF ingestion code is changed by this pass.
Citation-aware memory ingestion, source privacy policies, and independent
source-fact verification require coordinated AIOS server work.
