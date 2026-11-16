# Research sources (development)

Research is opt-in. Existing AIOS-Client installations have **no** granted research
sources. No Google, paid API, or SearXNG service is required to run the client.

## UI input normalization for LAN servers

In the setup page, source names such as `Calibre` and `SearXNG` are
normalized to the lowercase identifiers `calibre` and `searxng`. Spaces
become hyphens. Agent source permissions always reference these canonical IDs.

For Calibre OPDS, enter the Content Server base address, such as
`http://192.168.1.16/`; the browser editor and setup API append `/opds`
automatically. The current adapter requires an endpoint ending in `/opds`;
other OPDS paths would require an adapter change. The network test checks whether
the resulting URL returns an Atom feed; it does **not** prove that the
server's root page is an OPDS catalog. SearXNG accepts its base URL, for
example `http://192.168.1.4:8080/`, and uses `/search?format=json`.

The manually edited `research_sources.json` file remains strict: use
lowercase canonical IDs and the **complete** Calibre `/opds` endpoint.
UI normalization happens only when editing or testing an operator draft,
not when silently loading existing grant/ACL identifiers.

## Configure sources from the setup page or a JSON file

In the AIOS-Client setup page (normally port 8765), open **Research sources**.
Select **SearXNG**, **Calibre OPDS**, or a local-directory adapter; enter a
unique source ID and server URL, then **Save research source**. You may select a
saved source to edit it, **Test network connection** (SearXNG and Calibre), or
delete it when no agent still grants it. A disabled source cannot remain
granted. Save an agent profile with its allowed source checkboxes and choose
**Workspace** or **Research-only** under Tool mode. Stop and restart listeners
after changing source configuration or grants.

The UI and headless setups use the **same file**: `research_sources.json`
beside the main AIOS-Client configuration JSON. You can create or edit this
file using any text editor and click **Reload config file** in the setup UI.
A source file is optional until the first source is granted to an agent.
Example LAN configuration (replace the example addresses with IPs or DNS names
reachable **from the AIOS-Client host**):

```json
{
  "version": 1,
  "sources": [
    {
      "id": "home-search",
      "adapter": "searxng",
      "endpoint": "http://192.168.1.50:8080",
      "enabled": true,
      "priority": 10,
      "topics": ["general"]
    },
    {
      "id": "home-library",
      "adapter": "calibre_opds",
      "endpoint": "http://192.168.1.60:8080/opds",
      "enabled": true,
      "priority": 20,
      "topics": ["general"]
    }
  ]
}
```

The corresponding agent profile in the main configuration file may contain:

```json
"research_sources": ["home-search", "home-library"],
"tool_mode": "research"
```

`tool_mode: "workspace"` retains the existing general workspace tools and also
makes explicitly granted research tools available; `research` limits the model
to the research tool allowlist and supporting report/recall tools.

The network test makes one bounded read-only request: SearXNG's JSON search
endpoint or the configured Calibre OPDS catalog. It checks the response format
without following redirects, using ambient proxies, importing documents or
granting access. Calibre's endpoint **must end in /opds**. This first pass only
supports anonymous Calibre OPDS, not username/password-protected catalogs.
If a source is unreachable or JSON/XML support is missing, the test returns
a diagnostic status. The setup page is administrator-controlled; only configure
trusted network destinations. Network access grants do not authorize unrestricted
page fetching: SearXNG result-page reads still require explicit
`read_origins` entries.

The editor writes the JSON atomically with owner-only file permissions on POSIX
hosts. Disabling, renaming or deleting a source that is currently granted to an
agent is rejected until you update the affected profiles. Manual file edits can
still invalidate grants; the client fails closed when it loads such a profile.

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


## Hybrid discovery bridge v1 (feature branch, coordinated AIOS server)

With AIOS also running `feature/hybrid-research-discovery-v1`, workspace
protocol v3 advertises `research-discovery-v1`. A research-enabled worker
then gains an additional model-facing `research_nominate` tool. The tool
accepts an evidence ID from this run, an open AIOS dossier UUID owned by the
originating character, and a short rationale. It can only nominate evidence
that `research_read` successfully retrieved, not unexamined search snippets.

Nominations are delivered to the server under the existing **live run lease**
and recorded in `research_external_nomination`, not in the corpus or /char
memory. A source nomination is a retrieval record, **not a verified claim**.
The normal research report and memory candidate controls are unchanged.

Create a dossier on AIOS first, then supply its returned ID in the authorized
research assignment objective. No new research capability grants itself:
operator source grants and workspace work-admission policies still apply.
Older AIOS servers do not advertise this capability, so the extra client tool
is not registered. No operator credential is copied to the client.

The feature does not yet stage or upload complete documents. The next pass
can link accepted nominations to the existing AIOS corpus importer with a
separate content-transfer authorization boundary.


## Research execution control (experimental)

Research-only profiles now use a bounded per-run controller to avoid the
Calibre search/read loop observed on 2026-11-16. Defaults: at most 8 **unique**
normalized searches, 6 distinct network book acquisitions, 12 distinct passage
requests and 4 failed acquisitions. Exact duplicate searches/reads reuse
run-local results rather than spending another network request; successful
Calibre book downloads and extracted sections are cached for the run only.
The extracted book content is **not** added to the AIOS corpus or character
memory. A cache hit does not upgrade search snippets to read evidence.

Research-only runs use at most 20 model steps (or a smaller authorized assignment
budget), with up to 3 steps reserved for a structured final report. Once research
budgets are consumed, the model is directed to synthesize only the evidence it has
already received; no further tools are exposed for that reporting phase. If the
model still refuses to finish or returns invalid JSON, the host generates a
**partial/blocked evidence-only receipt report**, never fabricated research
findings. Host reports remain attributable worker evidence, not a geological
truth claim. Expired leases, cancellation and infrastructure failures may still
prevent final delivery; this is not a guarantee of success.

Calibre search results distinguish metadata fallback from full-text search
(`search_mode`), rank matching catalog metadata ahead of other hits, and report
provider errors as `source_error` rather than falsely reporting an empty
catalog. PDF/EPUB extraction remains capped at **6 MiB per book**, **80 pages
or sections**, and bounded text sizes. Increasing those limits or supporting
range-based PDF acquisition is deferred to a separate security/performance
review. PDF extraction installs fontTools and limits verbose pypdf logging;
malformed/no-text extraction is still reported as a failure or limitation.

Research evidence receipts preserve a bounded list of verified section
locations (`sections`), while `section` remains the latest location for
backward compatibility. Publication carries `sections_read`; citation
verification attests retrieval, **not** the truth or relevance of a finding.

Regression tests: `pytest -q tests/test_research_execution_control.py
tests/test_research_source_setup.py`. The tests exercise mocked, in-process
Calibre responses and do not need your LAN services. For an end-to-end run,
restart the listener after updating, submit a new bounded research task from
the character's chat, and compare search/read calls, total model steps, and
the delivered report against the previous baseline.
