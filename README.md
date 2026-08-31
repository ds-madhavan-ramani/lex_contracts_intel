# LEX — Legal EXtraction & Contract Intelligence

**LEX** is a Streamlit-in-Snowflake tool, backed by Snowflake Cortex AI, for
the MR5 Transition Contracts Team: enter a contract number, get its standard
questions answered — instantly, from history, if already extracted — with a
citation panel that opens the original document and highlights the exact
passage an answer came from, and a one-click PDF summary. It's built on
`project-llm-wiki`, the same reusable multi-project LLM Wiki engine that runs
[`ORG_MM_CHAT`](https://github.com/ds-madhavan-ramani/org_mm_chat) in
production — LEX is the second real project forked from it.

> **Note on the published Plan/Architecture docs** (`LEX_Delivery_Plan.html`,
> `LEX_Solution_Architecture.html`, linked at the bottom): they describe an
> earlier design centered on a free-form chatbot. The team has since
> confirmed there's no need for that — LEX is a contract-lookup-and-report
> tool instead (this README and the code reflect that). Those two documents
> are correct on the shared infrastructure (Snowflake account, security
> perimeter, database/compute-pool isolation) but stale on the application
> design; treat them as historical pending a refresh, not as the current
> spec.

## What it does

- **The Required Contracts Register**: an .xlsx the team maintains, listing
  every CW number LEX should have data for (2 today, growing toward 8 for
  Build/validation, more later). Uploaded on the Data Sources page, it's
  the authoritative source of which contract numbers exist — independent of
  which documents happen to be ingested yet.
- **Source documents**: signed/executed contracts, PDF (rarely DOCX) only,
  held on the team's SharePoint/network-drive folder. A file whose name or
  text doesn't show a marker like "Signed" or "Executed" is still ingested
  but flagged for a human to double-check — never silently dropped.
- **Contract Lookup** (the landing page): pick a contract number, and its
  standard questions are answered from **history**
  (`CONTRACT_FIELD_EXTRACTS`) instantly — nothing is re-parsed or
  re-extracted on a page view. A document is only ever re-parsed if its
  actual content (wording, dates, anything) changes; extraction is only
  ever re-run explicitly, when a linked document has changed since the last
  run.
- **Citations with a highlighted original**: click "View source" next to
  any answer and a side panel shows the exact cited passage (highlighted,
  always exact — built from text stored verbatim at extraction time) plus,
  for PDFs, the rendered original document with a best-effort highlight
  over the matching text.
- **Download as PDF**: a formatted one-page-per-contract summary — overview
  plus every standard question, its answer, confidence, and source — via
  `st.download_button`. The team will share example templates to match a
  house style; the current layout is a plain default, not a final design.
- **Contract families**: a signed contract is rarely one static document —
  its variations, extensions, and novation deeds are ingested as their own
  documents, then linked together on the Contract Register page so
  extraction considers a contract's full history, not just its base
  agreement.
- **No chatbot**: the free-form retrieval engine (`query_engine.search()`)
  still exists and is what the standard-question extraction runs on under
  the hood, but there's no chat UI — the team doesn't need one right now.

## How it works

```
Required Contracts Register (.xlsx)              SharePoint / network drive
        │  which CW numbers are in scope          (signed/executed PDFs)
        ▼                                                  │  Microsoft Graph API
CONTRACT_REGISTER  ◄──────────────────────────────────────-┘
   (LEXDB.DATA_LEX)        linked via CONTRACT_DOCUMENT_LINK
        │                          │
        │                          ▼
        │                  RAW_DOCUMENTS — AI_PARSE_DOCUMENT (OCR), PDF/DOCX
        │                  only, re-parsed only when content actually changes
        │                          │  index_builder.py (LEX_CONTRACT profile,
        │                          │  chunked for 100-500 page documents)
        │                          ▼
        │                  DOCUMENT_INDEX — clause/schedule-level tree
        │                          │
        │        contract_extraction.py: for each standard question, calls
        │        query_engine.search() scoped to the contract's linked
        │        documents (restrict_to_doc_ids) — same retrieval/citation
        │        engine chat would use, just with no chat UI on top
        ▼                          │
CONTRACT_FIELD_EXTRACTS  ◄─────────┘
  ("History") — answer, exact cited excerpt, a verified short highlight
  phrase, confidence, per (contract, field)
        │
        ├─ Contract Lookup page: read straight from here, instant
        ├─ citation_viewer.py + citation_panel_ui.py: presigned stage URL +
        │  client-side PDF.js render, best-effort highlight of the phrase
        └─ pdf_report.py: formats the same rows into a downloadable PDF
```

## Project configuration

| Setting | Value |
|---|---|
| Project code | `LEX` |
| Display name | LEX - Legal EXtraction & Contract Intelligence |
| Data database | `LEXDB` (dedicated — not the shared `MEDSOCMS`) |
| Data schema | `LEXDB.DATA_LEX` |
| Streamlit app | `MEDSOCMS.APP_CATALOG.LEX_APP` |
| Compute pool | `LEX_COMPUTE_POOL` (container runtime, dedicated) |
| Access | `LEX_USERS` role, granted only to named team members |
| Segmentation profile | `LEX_CONTRACT` (clause/schedule-aware, not generic prose sectioning) |
| Segmentation granularity | `DETAILED` (one section per clause, for precise citation in long documents) |
| Reranking | Enabled |
| Vector/semantic search | Enabled |
| Max candidate docs | `10` |
| Max document chars (= chunk size) | `100000` |
| Warehouse | `MTMWH02` (build-phase; swap to a production warehouse at go-live) |

These are all set in `pipeline/00_provision_project.ipynb`'s project-creation
cell — see that notebook for the full, current values and how to change
them.

## Prerequisites

1. Snowflake access to the `ADVANCEDANALYTICS` role and `MTMWH02` warehouse.
2. The `LEXDB` database and `LEX_COMPUTE_POOL` compute pool — typically
   `SYSADMIN`/`ACCOUNTADMIN`-only to create; the provisioning notebook
   attempts both and prints the exact statements to hand to an admin if it
   can't.
3. A Microsoft Graph API app registration with `Sites.Selected` permission
   granted on the contracts library. LEX defaults to reusing the same
   shared, tenant-level app registration `ORG_MM_CHAT` uses
   (`GRAPH_TENANT_ID`/`GRAPH_CLIENT_ID` in `python/config.py`, secret
   `MEDSOCMS.APP_CATALOG.GRAPH_API_SECRET`) — see `sql/test_graph_
   connectivity.sql` for how to give LEX its own dedicated, least-privilege
   registration instead once one exists.
4. The shared Graph API network rule + External Access Integration
   (`MEDSOCMS.APP_CATALOG.GRAPH_API_NETWORK_RULE`,
   `GRAPH_API_ACCESS_INTEGRATION`) — tenant-level, shared with every other
   project-llm-wiki project on this account, not created by this repo.
5. `fpdf2` (PDF export) resolves via PyPI on container runtime — no extra
   setup, but note it's deliberately **not** in `environment.yml` (would
   likely be unresolvable on warehouse runtime's Conda channel; see that
   file's own comment).

Run `sql/test_graph_connectivity.sql` to confirm the three Graph API
objects exist before deploying.

## Deploying / running

Everything is `pipeline/00_provision_project.ipynb`, run top to bottom in
Snowflake Notebooks. In order, it:

1. **Connects** to Snowflake.
2. **Sets up the shared catalog** (`MEDSOCMS.APP_CATALOG`) — skip if it
   already exists (it does, if `ORG_MM_CHAT` is already provisioned on this
   account).
3. **Creates `LEXDB` + `LEX_COMPUTE_POOL`** — prints clear next steps if the
   current role can't (see Prerequisites above).
4. **Creates the LEX project** — `LEXDB.DATA_LEX` schema/stage, registered
   in the catalog with its segmentation profile and retrieval settings.
   Confirm the SharePoint site/folder URLs before running this for real —
   left blank on purpose rather than guessed.
5. **Creates LEX's contract tables** — `CONTRACT_REGISTER`,
   `CONTRACT_DOCUMENT_LINK`, `CONTRACT_FIELD_EXTRACTS`.
6. **Creates the `LEX_USERS` role** and grants it to named team members —
   add usernames to the notebook's `NAMED_USERS` list and re-run any time
   membership changes.
7. **Deploys the app** — stages `python/` and `streamlit/` (flattened to
   the stage root — see the notebook's own comments for why a nested
   `MAIN_FILE` doesn't work), and runs `CREATE OR REPLACE STREAMLIT`.
8. **Schema migrations** — forward-only `ALTER TABLE ... ADD COLUMN IF NOT
   EXISTS` for a LEX project that existed before a given column did (a
   fresh provisioning run already has every column from step 4/5 above and
   these are no-ops for it).

Open the app: Snowsight → **Streamlit** → `LEX_APP`.

## Using the app

- **Contract Lookup** (`Chat.py` — the filename is a holdover from the
  template this was forked from; there's no chat feature) — the landing
  page. Pick a contract number, see its overview and standard questions
  answered from history, click **View source** on any answer to open the
  citation panel, and **Download PDF** for a formatted summary.
- **Data Sources** — three tabs: upload/sync the Required Contracts
  Register workbook; ingest contract PDFs/DOCX (upload or SharePoint,
  flagged if no signed/executed marker is found); manual index rebuild
  (rarely needed — ingestion indexes automatically).
- **Contract Register** — the admin view: link a contract's documents
  (base + variations/extensions/novations) into one family, run/re-run
  extraction, and review/verify every field with its citation.
- **Sync Status** — required-contracts coverage (how many are extracted,
  how many are current) plus raw ingestion/index counts and run history.

## Removing the project

```sql
CALL TEARDOWN_PROJECT('LEX', FALSE);  -- keep logs
CALL TEARDOWN_PROJECT('LEX', TRUE);   -- purge logs too
```

Drops the `LEX_APP` Streamlit app, its deploy stage, and
`LEXDB.DATA_LEX` — not the `LEXDB` database itself, which may hold other
objects.

## Under the hood: what's forked vs. new

The bulk of this repo — `sql/00_setup_catalog.sql`'s `PROJECTS` catalog and
`CREATE_PROJECT`/`TEARDOWN_PROJECT` procs, `python/query_engine.py`'s tree
search, the ingestion pipeline, the Streamlit deploy pattern — is the
`project-llm-wiki` engine forked from
[`ds-madhavan-ramani/org_mm_chat`](https://github.com/ds-madhavan-ramani/org_mm_chat),
carrying forward that project's hard-won lessons on Streamlit-in-Snowflake
deployment quirks, Cortex JSON-parsing robustness, and citation plumbing.
What's actually new for LEX:

| New | Why |
|---|---|
| `PROJECTS.DATA_DATABASE` (+ `CREATE_PROJECT`'s new parameter) | A project's data can now live in its own database (`LEXDB`), not just its own schema inside the shared `MEDSOCMS` |
| `PROJECTS.GRAPH_TENANT_ID` / `GRAPH_CLIENT_ID` / `GRAPH_SECRET_NAME` | Optional per-project dedicated Graph API app registration, for least-privilege ingestion, instead of always the shared tenant-level app |
| `LEX_CONTRACT` segmentation profile (`index_builder.py`) | Clause/schedule-aware sectioning for legal contracts |
| Chunked indexing (`index_builder.py`) | A 100–500 page contract doesn't fit in one indexing call |
| `query_engine.search()`'s `restrict_to_doc_ids` parameter | Scopes search to one contract's linked documents — what makes stock-field extraction just "search with the document set pre-selected" |
| `required_contracts.py` + Required Contracts Register upload | The authoritative "which CW numbers are in scope" list, seeded from an .xlsx, independent of what's been ingested |
| `contract_linking.py` / `contract_extraction.py` + `CONTRACT_REGISTER` / `CONTRACT_DOCUMENT_LINK` / `CONTRACT_FIELD_EXTRACTS` | Contract-family linking and the automated, persisted, cited standard-question extraction ("History") |
| `CONTRACT_FIELD_EXTRACTS.HIGHLIGHT_PHRASE` + `contract_extraction._extract_highlight_phrase` | A short exact quote, verified as a real substring, for the citation viewer to highlight |
| `CONTRACT_REGISTER.OVERVIEW_SUMMARY` + `contract_extraction.generate_contract_overview` | The longer-form contract overview shown on Contract Lookup and in the PDF |
| `citation_viewer.py` / `citation_panel_ui.py` | Presigned stage URLs + a hand-rolled client-side PDF.js viewer that best-effort highlights the cited passage in the original document |
| `pdf_report.py` | The downloadable Contract Summary PDF (`fpdf2`) |
| **Contract Lookup** page (`Chat.py`, repurposed) | The primary end-user surface — enter a contract number, get history, cite, export |
| **Contract Register** page | Admin: linking + verification workflow |

## Known limitations / unverified in this environment

Built and syntax-checked (all modules byte-compile and pass `pyflakes`;
`pdf_report.py` was smoke-tested locally against a real `fpdf2` install,
which caught and fixed two real bugs — `core_fonts_encoding` and
`multi_cell`'s cursor-position default), but **not** run against a live
Snowflake account from this environment:

- The PDF.js-based citation highlighting in `citation_viewer.py` is
  genuinely best-effort (OCR text doesn't always align character-for-
  character with the rendered page, and only horizontal/non-rotated text
  is handled) and has not been exercised in a real browser. The exact
  cited passage is always shown correctly as plain text regardless — only
  the highlight overlay on the rendered PDF is approximate.
- Whether Streamlit-in-Snowflake's Content-Security-Policy permits an
  embedded `components.v1.html` iframe to load a script from cdnjs is
  unverified — if blocked, the viewer's status line reports the failure
  and the always-working "Open in new tab" link (plain navigation, not a
  script) still gets the user to the source document.
- `GET_PRESIGNED_URL`'s stage argument is inlined as a literal (not a bind
  parameter) in `citation_viewer.py`, following the same pattern this
  codebase already confirmed is required for `BUILD_SCOPED_FILE_URL` —
  reasoned by analogy, not independently confirmed for this specific
  function.

## Open items

1. Confirm the "network drive" is actually a SharePoint library reachable
   via Graph API (assumed throughout this repo) — a raw file share needs a
   different ingestion bridge.
2. The 3–5 named users for the `LEX_USERS` role.
3. The team's example question/summary template — `contract_extraction
   .STOCK_FIELDS` currently has 15 questions; the team has said 16-20 is
   the real target.
4. Whatever identifies the BG/Cash securities-reconciliation list, for a
   future `SECURITIES_RECONCILIATION` view.
5. The existing CW-number formatting convention, so
   `contract_linking.suggest_cw_number()`'s auto-suggestion is reliable.
6. Confirmation that Cortex cross-region inference (to AWS AU) is
   acceptable for signed contract content, from a data-handling/compliance
   standpoint.

## Further reading

- **[LEX_Delivery_Plan.html](./LEX_Delivery_Plan.html)** ([published version](https://claude.ai/code/artifact/62475db9-d82d-41a0-b66e-1f85f2efbf4d))
  and **[LEX_Solution_Architecture.html](./LEX_Solution_Architecture.html)** ([published version](https://claude.ai/code/artifact/ef0664c5-268e-42b8-ab4e-553e9cbb785d))
  — infrastructure/security design, still current; application design is
  superseded by this README (see the note at the top).
