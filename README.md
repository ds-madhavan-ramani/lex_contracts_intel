# LEX — Legal EXtraction & Contract Intelligence

**LEX** is a Streamlit-in-Snowflake tool, backed by Snowflake Cortex AI, for
the MR5 Transition Contracts Team: enter a contract number, get its standard
questions answered — instantly, from history, if already extracted — laid
out exactly like the team's own **Contract Workspace Summary Template**,
with a citation panel that opens the original document and highlights the
exact passage an answer came from, and a one-click download of the summary
as a matching .docx. It's built on `project-llm-wiki`, the same reusable
multi-project LLM Wiki engine that already runs other projects in
production on this account — LEX is a project instance forked from it.

> **Note on the published Plan/Architecture docs** (`LEX_Delivery_Plan.html`,
> `LEX_Solution_Architecture.html`, linked at the bottom): they describe an
> earlier design centered on a free-form chatbot, an earlier infrastructure
> plan where LEX's catalog bookkeeping stayed in a database shared with
> other project-llm-wiki projects, and an assumed SharePoint/Graph API
> ingestion path. All three have since changed — LEX is a
> contract-lookup-and-report tool instead of a chatbot, it now runs fully
> self-contained (its own database, catalog, credentials, and compute pool,
> with no shared resources at all), and its contracts library was confirmed
> to be a genuine on-prem network drive (SMB), not SharePoint — this README
> and the code reflect all three changes. Treat those two documents as
> historical pending a refresh, not as the current spec.

> **Companion repo — [`lex_network_bridge`](https://github.com/ds-madhavan-ramani/lex_network_bridge)**:
> Snowflake's outbound network can't reach the MTM network drive directly
> (`metrotrains.local` isn't a directly resolvable/routable target from
> Snowflake — see Open Items below), so there is no direct-SMB ingestion
> path anywhere in this repo. Instead, a bridge tool runs on a Linux host
> *inside* the MTM network, pushing selected contract PDFs out to
> `MEDSCOMA.DATA_LEX.NETWORK_DRIVE_INBOX_STAGE` via `PUT`, one per-CW
> subfolder at a time. That tool lives entirely in that separate repo, not
> here. From there, a scheduled Task in *this* repo
> (`python/ingestion/stage_pickup.py`, `sql/04_stage_pickup_task.sql`)
> automatically picks staged files up into the normal ingest pipeline —
> `RAW_DOCUMENTS`, contract linking, indexing, and extraction — on a
> 5-minute schedule, no manual step required. See "Under the hood" below.

## What it does

- **The Required Contracts Register**: an .xlsx the team maintains
  (`MTM_CONTRACT_WORKSPACES.xlsx` — `CONTRACT_WORKSPACE_ID` /
  `CONTRACT_WORKSPACE_NAME` columns, one header row), listing every CW
  number LEX should have data for (2 today, growing toward 8 for
  Build/validation, more later). Uploaded on the Data Sources page, it's
  the authoritative source of which contract numbers exist — independent of
  which documents happen to be ingested yet — and seeds each contract's
  title too.
- **Source documents**: signed/executed contracts, PDF (rarely DOCX) only,
  held on the team's network drive — a genuine on-prem SMB file share,
  confirmed not SharePoint. Since Snowflake can't reach it directly, files
  arrive via the Data Sources page's Upload tab or the companion
  `lex_network_bridge` repo's automatic stage-pickup Task — see the note
  above. A file whose name or text doesn't show a marker like "Signed" or
  "Executed" is still ingested but flagged for a human to double-check —
  never silently dropped.
- **Contract Lookup** (the landing page): pick a contract number, and its
  standard questions are answered from **history**
  (`CONTRACT_FIELD_EXTRACTS`) instantly — nothing is re-parsed or
  re-extracted on a page view. A document is only ever re-parsed if its
  actual content (wording, dates, anything) changes; extraction is only
  ever re-run explicitly, when a linked document has changed since the last
  run.
- **The team's own Contract Workspace Summary Template, adopted exactly**:
  Contract detail (supplier, services, dates, value), an Executive
  Assessment narrative plus its 8-row findings table, a Commercial/
  Performance/Renewal Assessment table (8 more rows), Significant
  Variations (one line per linked amendment/extension/novation), a
  Consolidated Procurement Assessment scorecard (5 short ratings), and
  Recommended Actions — see `python/contract_extraction.py` and
  `assets/Contract_Workspace_Summary_Template.docx`.
- **Citations with a highlighted original**: click "View source" next to
  any finding and a side panel shows the exact cited passage (highlighted,
  always exact — built from text stored verbatim at extraction time) plus,
  for PDFs, the rendered original document with a best-effort highlight
  over the matching text.
- **Download as .docx**: the team's own template, filled in — same
  headings, same tables, same styles — via `python/docx_report.py`, which
  edits a live copy of `assets/Contract_Workspace_Summary_Template.docx`
  rather than building a document from scratch.
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
Required Contracts Register (.xlsx)   Upload tab      lex_network_bridge (SMB,
        │  which CW numbers          (signed/         separate repo, inside
        │  are in scope              executed PDFs)   the MTM network) → PUT
        ▼                                 │            → NETWORK_DRIVE_INBOX_STAGE
CONTRACT_REGISTER  ◄──────────────────────┴──────────────────┘  stage_pickup.py
   (MEDSCOMA.DATA_LEX)     linked via CONTRACT_DOCUMENT_LINK     (scheduled Task)
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
        │        documents (restrict_to_doc_ids), then synthesizes the
        │        Executive Assessment narrative, Recommended Actions, and
        │        classification scorecard from the extracted fields
        ▼                          │
CONTRACT_FIELD_EXTRACTS  ◄─────────┘
  ("History") — answer, exact cited excerpt, a verified short highlight
  phrase, confidence, per (contract, field); CONTRACT_REGISTER holds the
  three synthesized, contract-level outputs alongside it
        │
        ├─ Contract Lookup page: read straight from here, instant
        ├─ citation_viewer.py + citation_panel_ui.py: presigned stage URL +
        │  client-side PDF.js render, best-effort highlight of the phrase
        └─ docx_report.py: fills the team's own Contract Workspace Summary
           Template (assets/*.docx) with the same rows, unchanged styling
```

## The Contract Workspace Summary Template, mapped to the schema

`python/contract_extraction.py`'s field list mirrors
`assets/Contract_Workspace_Summary_Template.docx` exactly — same
groupings, same row labels — so `docx_report.py` can drop values straight
into the template's tables without reshuffling anything:

| Template section | Fields (`CONTRACT_FIELD_EXTRACTS.FIELD_KEY`) | How it's produced |
|---|---|---|
| Contract detail | `SUPPLIER`, `SERVICES`, `COMMENCEMENT`, `CURRENT_EXPIRY`, `CURRENT_VALUE` | Extracted (`query_engine.search()`, cited) |
| Executive Assessment — narrative | — | Synthesized from the extracted fields (`generate_contract_overview`) |
| Executive Assessment — table | `NOVATION_ASSIGNMENT`, `CONFIDENTIALITY_DISCLOSURE`, `TERM_AND_EXTENSIONS`, `COMPLEXITY`, `SEPARABLE_PORTIONS`, `PAYMENT_REGIME`, `SECURITY`, `DEFECTS_LIABILITY` | Extracted, cited |
| Significant Variations | — | Read from `CONTRACT_DOCUMENT_LINK` (non-BASE roles) + each document's own `DOCUMENT_INDEX` summary — no extra Cortex call |
| Commercial, Performance and Renewal Assessment | `PRICE_REVIEW`, `EA_LABOUR_EXPOSURE`, `KPI_FRAMEWORK`, `COMMERCIAL_CONSEQUENCES`, `TERMINATION`, `AUTO_RENEWAL_PERPETUAL_TERM`, `CHANGE_OF_CONTROL`, `CURRENT_STATUS` | Extracted, cited |
| Consolidated Procurement Assessment (scorecard) | `OVERALL_CLASSIFICATION`, `NOVATION_DISCLOSURE_RATING`, `COMMERCIAL_MODEL_RATING`, `OPERATIONAL_EXPOSURE_RATING`, `RENEWAL_POSITION_RATING` | Synthesized from the extracted fields — deliberately *not* independently re-searched, so it can't disagree with the detailed tables above (`generate_classification_scorecard`) |
| Recommended Actions | — | Synthesized from the extracted fields (`generate_recommended_actions`); an empty list if nothing warrants flagging |

21 extracted fields today. The template can grow — add a
`(FIELD_KEY, question)` pair to `contract_extraction._QUESTIONS`, put the
key in whichever `*_FIELDS` group matches where it belongs, and add its
label to `FIELD_LABELS` — no other code changes needed, as long as the
`.docx` template itself gains a matching row (`docx_report.py` matches
table rows by their own label text, not position).

## Project configuration

| Setting | Value |
|---|---|
| Project code | `LEX` |
| Display name | LEX - Legal EXtraction & Contract Intelligence |
| Catalog database | `MEDSCOMA` (LEX's own — no shared database anywhere) |
| Catalog schema | `MEDSCOMA.APP_CATALOG` (`PROJECTS`, `PROJECT_SYNC_LOG`, `PROJECT_QUERY_LOG`) |
| Data database | `MEDSCOMA` |
| Data schema | `MEDSCOMA.DATA_LEX` |
| Streamlit app | `MEDSCOMA.APP_CATALOG.LEX_APP` |
| Compute pool | `STREAMLIT_COMPUTE_POOL_CONTRACT_MGMT` (container runtime, dedicated, `MIN_NODES=1 MAX_NODES=2`) |
| Access | `LEX_USERS` role, granted to `ADVANCEDANALYTICS` via role hierarchy — actual membership managed by a security group, not per-user grants |
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

Everything LEX reads or writes lives in `MEDSCOMA` — its catalog
(`APP_CATALOG`, forked from the project-llm-wiki template's usual pattern
of centralizing that in a shared `MEDSOCMS` database used by every project
on the account), its data (`DATA_LEX`), and its Streamlit app/stage. LEX
holds no reference to `MEDSOCMS`, to any other project-llm-wiki project's
resources, or to a compute pool other than its own dedicated
`STREAMLIT_COMPUTE_POOL_CONTRACT_MGMT`.

## Prerequisites

1. Snowflake access to the `ADVANCEDANALYTICS` role and `MTMWH02` warehouse.
2. The `MEDSCOMA` database and `STREAMLIT_COMPUTE_POOL_CONTRACT_MGMT` compute pool — typically
   `SYSADMIN`/`ACCOUNTADMIN`-only to create; the provisioning notebook
   attempts both and prints the exact statements to hand to an admin if it
   can't.
3. `python-docx` and `reportlab` (the Word/PDF summary exports) resolve
   via PyPI on container runtime — no extra setup, but note both are
   deliberately **not** in `environment.yml` (would likely be
   unresolvable on warehouse runtime's Conda channel; see that file's own
   comment).

There is no direct-SMB ingestion path in this codebase — LEX's contracts
library is a genuine on-prem network drive, but Snowflake can't reach it
directly (see Open Items below), so documents arrive via upload or the
companion `lex_network_bridge` repo's stage-pickup Task instead. Nothing
network-drive-credential-related needs setting up here.

## Deploying / running

Everything is `pipeline/00_provision_project.ipynb`, run top to bottom in
Snowflake Notebooks. In order, it:

1. **Connects** to Snowflake.
2. **Sets up LEX's own catalog schema** (`MEDSCOMA.APP_CATALOG`) — skip if
   it already exists (e.g. a re-run after the first provisioning pass).
3. **Creates `MEDSCOMA` + `STREAMLIT_COMPUTE_POOL_CONTRACT_MGMT`** — prints clear next steps if the
   current role can't (see Prerequisites above).
4. **Creates the LEX project** — `MEDSCOMA.DATA_LEX` schema/stage, registered
   in the catalog with its segmentation profile and retrieval settings.
5. **Sets the network drive location** (`NETWORK_DRIVE_HOST`/`SHARE`/
   `DEFAULT_PATH`/`DOMAIN` on the `PROJECTS` row) — not read by this repo's
   own code; it's shared config storage the companion `lex_network_bridge`
   repo queries directly for its real SMB connection. Skip if already set.
6. **Creates LEX's contract tables** — `CONTRACT_REGISTER`,
   `CONTRACT_DOCUMENT_LINK`, `CONTRACT_FIELD_EXTRACTS`.
7. **Deploys the app** — stages `python/` (structure preserved),
   `streamlit/` (flattened to the stage root — see the notebook's own
   comments for why a nested `MAIN_FILE` doesn't work), **and `assets/`**
   (structure preserved, as a sibling of `python/` — this is what
   `docx_report.py` finds the Word template through), then runs
   `CREATE OR REPLACE STREAMLIT`.
8. **Sets up the stage pickup task** — runs `sql/04_stage_pickup_task.sql`,
   the scheduled Task that drains `NETWORK_DRIVE_INBOX_STAGE` (filled by
   the companion `lex_network_bridge` repo) into the normal ingest
   pipeline automatically. Must run after step 7 — its stored procedure
   imports the `python/` tree that step just staged.
9. **Creates the `LEX_USERS` role** and grants it once to `ADVANCEDANALYTICS`
   — actual user access is managed externally via a security group, not
   per-user grants in this notebook. Note this gives LEX access to
   everyone who holds `ADVANCEDANALYTICS`, not just a named handful. Runs
   after the deploy step since `GRANT USAGE ON STREAMLIT` needs the app
   object to already exist.
10. **Schema migrations** — forward-only `ALTER TABLE ... ADD COLUMN IF NOT
   EXISTS` (plus a one-off `SHAREPOINT_ITEM_ID -> SOURCE_ITEM_ID` rename
   and the `CONTRACT_OUTPUT_STAGE` cache stage)
   for a LEX project that existed before a given column/stage did (a fresh
   provisioning run already has everything from step 4/5 above and
   these are no-ops for it).

Open the app: Snowsight → **Streamlit** → `LEX_APP`.

## Using the app

- **Contract Lookup** (`Chat.py` — the filename is a holdover from the
  template this was forked from; there's no chat feature) — the landing
  page. Pick a contract number, see its Executive Assessment and every
  section of the Contract Workspace Summary Template answered from
  history, click **View source** on any finding to open the citation
  panel, and download the summary as **Word** or **PDF**. Both are served
  from `contract_output_cache`'s stage cache — already produced by the
  async stage-pickup Task or a prior extraction run — falling back to
  building the file on the spot if nothing's cached yet.
- **Data Sources** — three tabs: upload/sync the Required Contracts
  Register workbook; upload contract PDFs/DOCX (flagged if no
  signed/executed marker is found); manual index rebuild (rarely needed —
  ingestion indexes automatically). A document can also arrive
  automatically via the companion `lex_network_bridge` repo's stage-pickup
  Task — no UI here for that path, it just shows up already ingested.
- **Contract Register** — the admin view: link a contract's documents
  (base + variations/extensions/novations) into one family, run/re-run
  extraction, and review/verify every field with its citation.
- **Sync Status** — required-contracts coverage (how many are extracted,
  how many are current) plus raw ingestion/index counts and run history.
  **Check for new files now** forces a stage-pickup run on the spot
  (`CALL RUN_LEX_STAGE_PICKUP()`) instead of waiting for the 5-minute
  scheduled Task — useful right after staging files via the companion
  `lex_network_bridge` repo.

## Removing the project

```sql
CALL TEARDOWN_PROJECT('LEX', FALSE);  -- keep logs
CALL TEARDOWN_PROJECT('LEX', TRUE);   -- purge logs too
```

Drops the `LEX_APP` Streamlit app, its deploy stage, and
`MEDSCOMA.DATA_LEX` — not the `MEDSCOMA` database itself, which may hold other
objects.

## Under the hood: what's forked vs. new

The bulk of this repo — `sql/00_setup_catalog.sql`'s `PROJECTS` catalog and
`CREATE_PROJECT`/`TEARDOWN_PROJECT` procs, `python/query_engine.py`'s tree
search, the ingestion pipeline, the Streamlit deploy pattern — is the
`project-llm-wiki` engine, carrying forward that template's hard-won
lessons on Streamlit-in-Snowflake deployment quirks, Cortex JSON-parsing
robustness, and citation plumbing. What's actually new for LEX:

| New | Why |
|---|---|
| `PROJECTS.DATA_DATABASE` (+ `CREATE_PROJECT`'s new parameter) | A project's data can now live in its own database (`MEDSCOMA`), not just its own schema inside the shared `MEDSOCMS` this template otherwise defaults to — LEX also moved its catalog schema itself into `MEDSCOMA`, so it holds no shared database at all |
| `LEX_CONTRACT` segmentation profile (`index_builder.py`) | Clause/schedule-aware sectioning for legal contracts |
| Chunked indexing (`index_builder.py`) | A 100–500 page contract doesn't fit in one indexing call |
| `query_engine.search()`'s `restrict_to_doc_ids` parameter | Scopes search to one contract's linked documents — what makes stock-field extraction just "search with the document set pre-selected" |
| `required_contracts.py` + Required Contracts Register upload | The authoritative "which CW numbers are in scope" list, seeded (number + title) from `MTM_CONTRACT_WORKSPACES.xlsx`, independent of what's been ingested |
| `contract_linking.py` / `contract_extraction.py` + `CONTRACT_REGISTER` / `CONTRACT_DOCUMENT_LINK` / `CONTRACT_FIELD_EXTRACTS` | Contract-family linking and the automated, persisted, cited standard-question extraction ("History"), with its field list matching the team's own template |
| `CONTRACT_FIELD_EXTRACTS.HIGHLIGHT_PHRASE` + `contract_extraction._extract_highlight_phrase` | A short exact quote, verified as a real substring, for the citation viewer to highlight |
| `CONTRACT_REGISTER.OVERVIEW_SUMMARY` / `RECOMMENDED_ACTIONS` / `CLASSIFICATION_SCORECARD` | The template's Executive Assessment narrative, Recommended Actions list, and Consolidated Procurement Assessment scorecard — all synthesized from the extracted fields, not independently re-derived |
| `ingestion/stage_pickup.py` + `sql/04_stage_pickup_task.sql` | A scheduled Snowflake Task that drains `NETWORK_DRIVE_INBOX_STAGE` (filled by the companion `lex_network_bridge` repo) into `RAW_DOCUMENTS` → linking → indexing → extraction, automatically. Auto-linking is safe here specifically because the CW number comes from the bridge's per-CW staging subfolder — a human already confirmed it by searching that folder — not a filename guess, which is what `contract_linking.suggest_cw_number()` deliberately never does unattended elsewhere in this codebase. A file is `REMOVE`d from the inbox once fully handled (success or an unchanged duplicate), so the OCR/parse step never repeats on the same file every 5-minute tick — only a `FAILED` file stays for the next tick to retry |
| `citation_viewer.py` / `citation_panel_ui.py` | Presigned stage URLs + a hand-rolled client-side PDF.js viewer that best-effort highlights the cited passage in the original document |
| `assets/Contract_Workspace_Summary_Template.docx` + `docx_report.py` | The team's actual Word template, filled in place (structure/styles preserved) rather than a bespoke document built from scratch |
| `pdf_report.py` | The same Contract Workspace Summary content as a PDF, rendered independently with `reportlab` (pure Python, no LibreOffice/Word on the compute pool or in a stored procedure) rather than converting the `.docx` |
| `contract_output_cache.py` + `CONTRACT_OUTPUT_STAGE` | Caches each contract's Word/PDF summary to a stage as soon as extraction finishes — Task-driven or manual — so download buttons serve a pre-built file instead of regenerating it on every page view; falls back to a live build on a cache miss |
| **Contract Lookup** page (`Chat.py`, repurposed) | The primary end-user surface — enter a contract number, get history, cite, export |
| **Contract Register** page | Admin: linking + verification workflow |

## Known limitations / unverified in this environment

Built and syntax-checked (all modules byte-compile and pass `pyflakes`),
and `docx_report.py` was smoke-tested locally against the real bundled
template with `python-docx` installed — including the empty-data edge
case (no extraction yet, no variations, no recommended actions) — which is
what caught and fixed a real title-construction bug (duplicating the
supplier name) before it shipped. Not run against a live Snowflake account
from this environment, though:

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
- `docx_report.py` locates the template's tables/headings by matching text
  (row labels, heading text) rather than fixed positions, and raises a
  clear `TemplateStructureError` naming what it couldn't find if the
  template is ever edited in a way that removes one of those markers —
  but a *cosmetic* template edit that keeps every marker intact is
  untested beyond the one template file bundled in `assets/`.
- `sql/04_stage_pickup_task.sql`: one assumption confirmed on a live
  account, two bugs found and fixed in the process. Confirmed: the
  procedure's `PACKAGES` clause resolves `python-docx` and `reportlab`
  from this account's Anaconda channel fine (needed for
  `contract_output_cache.py`'s Word/PDF caching). Fixed: (1) `CREATE
  TEMPORARY TABLE` inside a Python stored procedure raises "Unsupported
  statement type 'temporary TABLE'" — the stream-consumption step now
  uses a permanent table (`CREATE OR REPLACE`, so it never accumulates;
  it lives in `DATA_LEX` so project teardown removes it automatically);
  (2) `IMPORTS` given a bare stage *directory* does **not** flatten that
  directory's contents onto `sys.path` the way Streamlit's own
  `sys.path.insert()` does — confirmed as `ModuleNotFoundError: No module
  named 'ingestion'` on the very first import. Fixed by having the "Set
  up the stage pickup task" notebook cell zip `python/`'s contents (files
  at the zip root, matching Streamlit's own import layout) and pointing
  `IMPORTS` at that zip instead — Snowflake's documented, reliable way to
  import a multi-file/multi-package Python tree into a stored procedure.
  Also confirmed and fixed: `NETWORK_DRIVE_INBOX_STAGE`'s directory table
  doesn't reliably auto-refresh the moment a new file is `PUT` — files
  were visible via `LIST` but absent from `DIRECTORY()` (what
  `list_staged_files()` queries) until an explicit `ALTER STAGE ...
  REFRESH`. `list_staged_files()` now runs that refresh itself on every
  call — cheap (metadata-only) and removes an entire class of "why didn't
  my new file show up" confusion. A second layer of the same symptom
  surfaced even after that fix was deployed: the procedure always runs as
  its owner role (`EXECUTE AS OWNER`, Snowflake's default), and Snowflake's
  persisted query result cache is keyed by exact query text *and* role —
  `ALTER STAGE ... REFRESH` is a stage-metadata operation, not a table
  write, so it isn't guaranteed to invalidate a cached result from an
  earlier identical query under that same role, even though a different
  role's session sees the fresh data immediately. `list_staged_files()`
  now also bypasses the cache for that one query. The obvious way —
  `ALTER SESSION SET USE_CACHED_RESULT = FALSE` — turned out to be a
  second unsupported statement type inside a Python stored procedure
  ("Unsupported statement type 'ALTER_SESSION'"), same class of
  restriction as `CREATE TEMPORARY TABLE` above. The next attempt —
  `statement_params={"use_cached_result": False}` on that one query's
  `.collect()` — ran without error but, confirmed on a live account,
  still didn't fix the "0 files found" symptom, which was the first clue
  the diagnosis was wrong. `list_staged_files()` was then made to append a
  fresh UUID SQL comment to the query text on every call, guaranteeing no
  pre-existing cached result could ever be served — and the symptom
  *still* didn't change, which, combined with a live account confirming
  `DIRECTORY()` returns the correct rows both from a plain worksheet and
  from a minimal isolated stored procedure, proved conclusively that
  caching was never the cause. **The actual root cause**: files were
  landing at the inbox stage's ROOT, with no `<CW_NUMBER>/` subfolder at
  all — `list_staged_files()`'s own deliberate "skip a file with no CW
  folder rather than guess" safety check was filtering out every file,
  correctly by its own logic, which is what produced "0 files found" even
  though `DIRECTORY()` was returning real rows the whole time. This
  recurred even though the CW-subfolder staging convention was already
  fixed in the companion `lex_network_bridge` repo, most likely because
  that fix doesn't cover every one of the bridge tool's upload paths (its
  browser app has a human pick the CW folder; its CLI has a "best-effort
  regex fallback" that can miss). Fixed here defensively: a root-level
  file whose name starts with `CW<digits> -` (the bridge tool's own
  filename convention) is now treated as reliably CW-attributed as a
  folder name would be, and is moved into the matching subfolder
  automatically before being picked up — a root file that doesn't match
  this pattern is still skipped with a warning rather than guessed at.
  The cache-busting UUID comment stays in place; it's cheap and defends
  against the theoretical caching failure mode even though it turned out
  not to be the actual bug this time. That root-file normalization fix
  immediately exposed a second, previously-latent bug on the very first
  live run: its `REMOVE @{stage}/{filename}` call built the stage
  location by directly interpolating the raw filename into the SQL text.
  Real contract filenames (`CW20841 - Executed Services Agreement...
  (TRAINS).pdf`) have spaces, hyphens, and parens, and Snowflake parses
  an unquoted `@stage/path` token-by-token — confirmed on a live account
  as a SQL compilation error at the first space-hyphen-space. Fixed by
  wrapping the whole `@stage/path` in single quotes (Snowflake's own
  documented form for this, e.g. `REMOVE '@%mytable/myfile.csv.gz'`),
  doubling any embedded single quote per standard SQL string-literal
  escaping (see `_quoted_stage_location` in `ingestion/stage_pickup.py`
  — unrelated to the backslash-doubling gotcha documented elsewhere in
  this file for `NETWORK_DRIVE_DEFAULT_PATH`, which is a different
  escape rule for a different character). The identical unquoted pattern
  existed in `_remove_from_inbox()`'s per-file cleanup too — fixed there
  as well, even though it hadn't been exercised yet at the time, since it
  would have failed the same way. One assumption remains genuinely
  unverified: `COPY FILES INTO <stage> FROM <stage>` (stage-to-stage, used
  in `ingestion/stage_pickup.py` to avoid a GET/PUT round trip) behaves as
  documented — it has a documented fallback in that module's own docstring
  if it doesn't hold.
- The scheduled Task's *automatic* execution (as opposed to a manual
  `CALL RUN_LEX_STAGE_PICKUP()`) is separately unverified — `EXECUTE
  TASK` is commonly an `ACCOUNTADMIN`-only privilege to grant (like
  `CREATE ROLE` was for this account's `ADVANCEDANALYTICS`), and a task
  that's `CREATE`d and `RESUME`d without it will simply never fire on its
  own schedule, with no error surfaced anywhere obvious. Confirm with
  `SHOW TASKS LIKE 'LEX_STAGE_PICKUP_TASK'` (check `state` is `started`)
  and `SELECT * FROM TABLE(INFORMATION_SCHEMA.TASK_HISTORY(TASK_NAME =>
  'LEX_STAGE_PICKUP_TASK')) ORDER BY SCHEDULED_TIME DESC` (confirms it has
  actually fired) once a new file is staged — if it never appears, hand
  `GRANT EXECUTE TASK ON ACCOUNT TO ROLE ADVANCEDANALYTICS;` to whoever
  holds `ACCOUNTADMIN`.
- `pdf_report.py` renders the same fields/tables/bullets `docx_report.py`
  does, independently, with `reportlab` — verified structurally (valid PDF
  header, non-trivial size, builds without error against the full field
  set including the empty-data edge case) but not visually proofed in a
  PDF viewer from this environment, and not a pixel-for-pixel match of the
  Word template's styling by design (see that module's own docstring).

## Open items

1. **Settled, not pursuing further**: `metrotrains.local` is not directly
   DNS-resolvable from Snowflake — `CREATE NETWORK RULE ... VALUE_LIST =
   ('metrotrains.local:445')` fails with "invalid value ... unresolvable
   host name." Further confirmed (via the `lex_network_bridge` repo's own
   work, run from inside the MTM network): `apps$` is a domain-based DFS
   namespace, not a single file server — the real target, revealed by an
   SMB client's own referral-following logs, is
   `MTADFS201V.metrotrains.local`. Rather than pursue direct Snowflake
   connectivity to it (a real IP/FQDN, or a DNS forwarder for the internal
   zone via Private Link), this repo's direct-SMB ingestion path
   (`utils/network_drive_client.py`, `ingestion/network_drive_ingest.py`,
   and the Data Sources page's old "Network Drive" tab) has been removed
   entirely — `lex_network_bridge` is the permanent way to get files into
   `NETWORK_DRIVE_INBOX_STAGE`, not a stopgap. `PROJECTS.NETWORK_DRIVE_HOST`
   / `NETWORK_DRIVE_SHARE` / `NETWORK_DRIVE_DEFAULT_PATH` / `NETWORK_DRIVE_DOMAIN`
   are the one exception: **still present, and load-bearing** — nothing in
   *this* repo reads them, but `lex_network_bridge`'s own
   `network_drive_to_stage.py` queries this exact `PROJECTS` row directly
   for its real SMB host/share/domain (confirmed values: host
   `MTADFS201V.metrotrains.local`, share `apps$`, domain `METROTRAINS`).
   An earlier pass at this cleanup dropped these columns, silently
   deleting that working configuration and breaking the bridge tool with
   no error until its next run — do not drop them again. (`NETWORK_DRIVE_SECRET_NAME`
   *was* removed for good — it only ever backed the deleted in-app
   Streamlit SECRET binding; the bridge tool gets its own SMB credentials
   from local environment variables on the bridge host, never from this
   table.)
2. Confirm the security group behind `ADVANCEDANALYTICS` is scoped to the
   right population — `LEX_USERS` is granted to that role directly, so
   whoever it's provisioned to gets LEX access.
3. Whatever identifies the BG/Cash securities-reconciliation list, for a
   future `SECURITIES_RECONCILIATION` view.
4. The existing CW-number formatting convention, so
   `contract_linking.suggest_cw_number()`'s auto-suggestion is reliable.
5. Confirmation that Cortex cross-region inference (to AWS AU) is
   acceptable for signed contract content, from a data-handling/compliance
   standpoint.

## Further reading

- **[LEX_Delivery_Plan.html](./LEX_Delivery_Plan.html)** ([published version](https://claude.ai/code/artifact/62475db9-d82d-41a0-b66e-1f85f2efbf4d))
  and **[LEX_Solution_Architecture.html](./LEX_Solution_Architecture.html)** ([published version](https://claude.ai/code/artifact/ef0664c5-268e-42b8-ab4e-553e9cbb785d))
  — infrastructure/security design, still current; application design is
  superseded by this README (see the note at the top).
