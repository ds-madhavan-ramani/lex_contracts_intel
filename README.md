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
        │        classification scorecard from the extracted fields.
        │        A contract can have 2-20+ linked files (base, variations,
        │        extensions, renewals) — search()'s contract_id parameter
        │        tags each excerpt with its recency within that family
        │        (EFFECTIVE_DATE if set, else SEQUENCE_NO, else DOC_ROLE)
        │        and tells synthesis to prefer the more recent document
        │        when documents disagree, so a later variation's restated
        │        expiry/value supersedes an earlier one instead of the
        │        answer depending on which excerpt retrieval happened to
        │        rank higher
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
| Contract detail | `SUPPLIER`, `SERVICES`, `COMMENCEMENT`, `CURRENT_EXPIRY`, `CURRENT_VALUE` | Extracted — one of `FIELD_GROUPS`'s full-document agents, cited (see "Stock-field extraction reads full documents directly" below) |
| Executive Assessment — narrative | — | Synthesized from the extracted fields (`generate_contract_overview`) |
| Executive Assessment — table | `NOVATION_ASSIGNMENT`, `CONFIDENTIALITY_DISCLOSURE`, `TERM_AND_EXTENSIONS`, `COMPLEXITY`, `SEPARABLE_PORTIONS`, `PAYMENT_REGIME`, `SECURITY`, `DEFECTS_LIABILITY` | Extracted, cited |
| Significant Variations | — | Read from `CONTRACT_DOCUMENT_LINK` (non-BASE roles) + each document's own `DOCUMENT_INDEX` summary — no extra Cortex call |
| Commercial, Performance and Renewal Assessment | `PRICE_REVIEW`, `EA_LABOUR_EXPOSURE`, `KPI_FRAMEWORK`, `COMMERCIAL_CONSEQUENCES`, `TERMINATION`, `AUTO_RENEWAL_PERPETUAL_TERM`, `CHANGE_OF_CONTROL`, `CURRENT_STATUS` | Extracted, cited |
| Consolidated Procurement Assessment (scorecard) | `OVERALL_CLASSIFICATION`, `NOVATION_DISCLOSURE_RATING`, `COMMERCIAL_MODEL_RATING`, `OPERATIONAL_EXPOSURE_RATING`, `RENEWAL_POSITION_RATING` | Synthesized from the extracted fields — deliberately *not* independently re-searched, so it can't disagree with the detailed tables above (`generate_classification_scorecard`) |
| Key Commercial Risks / Procurement Recommendation / Retender Strategy | `KEY_COMMERCIAL_RISKS`, `PROCUREMENT_RECOMMENDATION`, `RETENDER_STRATEGY` | Synthesized from the extracted fields, same reasoning as the scorecard above (`generate_procurement_strategy`) — added to match a forward-looking wrap-up the CoPilot-generated reference summary CW20841 was benchmarked against included and this app's own output didn't |
| Recommended Actions | — | Synthesized from the extracted fields (`generate_recommended_actions`); an empty list if nothing warrants flagging |

21 extracted fields + 3 synthesized wrap-up fields today. The template
can grow — add a `(FIELD_KEY, question)` pair to
`contract_extraction._QUESTIONS`, put the key in whichever `*_FIELDS`
group matches where it belongs, and add its label to `FIELD_LABELS` — no
other code changes needed, as long as the `.docx` template itself gains a
matching row (`docx_report.py` matches table rows by their own label
text, not position).

### Condensed (~2-page) output, alongside the full report

Every download button set now offers two pairs, not one: the full
Contract Workspace Summary (docx/pdf, unchanged) and a condensed pair
(`docx_condensed`/`pdf_condensed` in `contract_output_cache.py`'s
`_BUILDERS`) — same section order, same underlying data, but each of the
21 stock fields is rendered from a one-to-two-sentence condensed version
(`CONTRACT_REGISTER.CONDENSED_FIELDS`, see
`contract_extraction.generate_condensed_summary`) instead of its full
paragraph-length `FIELD_VALUE`, Significant Variations summaries are
truncated, and Recommended Actions AND Key Commercial Risks are each
capped to their top 4-5 items — in the same spirit as the CoPilot-
generated reference summary CW20841 was benchmarked against, which is
itself only about a page long.
`generate_condensed_summary` is a single cheap synthesis call (it
compresses already-extracted text, not raw documents, so its cost doesn't
scale with the size of the contract's document family) run once per
extraction alongside the other four synthesis steps. The condensed
`.docx` is a fresh `python-docx` document, not built from
`assets/Contract_Workspace_Summary_Template.docx` — there's no bundled
short-form template to match, unlike the full report. UNVERIFIED: actual
page count depends on the real Word/PDF renderer's line-wrapping for real
contract data; a mock run with representative-length fake data rendered
to 2 pages via reportlab's own pagination (checked by counting `/Type
/Page` objects in the raw PDF bytes), which is encouraging but not a
substitute for checking a real extraction's condensed output directly.

All four outputs (`.docx`/`.pdf` × full/condensed) now repeat the
contract's CW number and title in a running header, and which of the two
lengths it is ("Detailed Summary" / "Condensed Summary") in a running
footer, on every page — `docx_report._set_header_footer` for both `.docx`
builders, `pdf_report._page_header_footer` (a reportlab canvas callback,
since `SimpleDocTemplate`'s flowable content has no built-in running
header/footer concept) for both `.pdf` builders. CONFIRMED bug this
fixes, not just a missing feature: the bundled template
(`assets/Contract_Workspace_Summary_Template.docx`) already had a
header/footer, but its placeholder text was hardcoded to the literal CW
number/wording from whichever contract the template was originally
authored against ("CW20841 | Contract Review Summary" /
"Commercial review summary | ...") — every generated `.docx` for every
OTHER contract was silently carrying that same wrong CW number in its
header, not an obviously-blank placeholder. `build_contract_docx` never
touched the header/footer before this fix, so the bug was invisible
unless someone happened to compare two different contracts' downloaded
files side by side. The PDF header/footer's actual rendered content is
UNVERIFIED from this environment — no PDF-parsing library is available
here to extract and check drawn canvas text, so this was checked only as
"the build doesn't crash and the page count is unaffected," not visually
proofed in a real PDF viewer.

The full and condensed outputs' underlying CONTENT is identical by
construction, in both formats — all four builders read from the exact
same `contract_linking.get_contract`/`contract_extraction.get_contract_fields`
calls (condensed field values additionally reading
`CONTRACT_REGISTER.CONDENSED_FIELDS`) rather than each assembling its own
copy of the data — so a report/word discrepancy would be a real bug in
one specific builder, not an architectural gap. What legitimately differs
between `.docx` and `.pdf` of the SAME length is visual styling only
(the Word template's own formatting vs. reportlab's independent layout —
see `pdf_report.py`'s own module docstring), which is by design, not a
defect.

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

The app is four pages, listed in the sidebar in the order you'd normally
touch them: **Data Sources** (get documents in) → **Sync Status** (watch
ingestion/indexing/extraction run, and force a run on demand) →
**Contract Register** (link documents into a contract family, run
extraction, review/verify) → **Contract Lookup** (the everyday landing
page — look a contract up, read its answers, download the summary). You
won't touch all four every time; which ones you need depends on whether
documents are arriving via the companion `lex_network_bridge` repo or a
manual upload.

### Which path applies to you

- **Documents arrive via `lex_network_bridge`** (the normal path once
  it's set up) — copy files into the network drive folder the bridge
  watches, one CW-numbered subfolder per contract, and the rest is
  driven from **Sync Status**. See "Workflow A" below.
- **You have a file on hand right now and don't want to wait for the
  bridge** — upload it directly on **Data Sources**. See "Workflow B"
  below. The two paths converge at the same `RAW_DOCUMENTS` table and
  can be mixed freely (some contracts via the bridge, one-off files via
  upload).

### Workflow A — documents via `lex_network_bridge`

1. **Copy the files** into the bridge's watched folder, in a subfolder
   named exactly for the CW number (e.g. `CW12345/CW12345 - Executed
   Services Agreement.pdf`, plus any amendment/variation deeds in that
   same subfolder). The bridge pushes these into
   `NETWORK_DRIVE_INBOX_STAGE` on its own schedule.
2. *(Optional)* If any of these are brand-new CW numbers, add them to
   the Required Contracts Register workbook and upload it on
   **Data Sources → 📋 Required Contracts Register tab → "Sync
   register."** Not required — `stage_pickup` auto-creates the contract
   register row from the folder name either way — but this gives it a
   proper title immediately instead of "(title not yet set)."
3. **Trigger the pickup.** Either wait for the scheduled Task (every 5
   minutes), or go to **Sync Status → 🔄 "Check for new files now"** for
   an immediate run with a live log. One click runs the whole pipeline
   per file: drain the inbox → OCR/parse → auto-link to its contract
   (first file for a CW = BASE, later ones = VARIATION) → index →
   extract every contract touched this run → cache the Word/PDF outputs
   (full and 2-page condensed, both formats).
4. **Watch it run.** The log box shows per-file progress
   (`[i/n] filename — parsing…` → `INGESTED (BASE/VARIATION)`), then
   per-document indexing progress, then per-contract extraction
   progress (`Extraction [n/N]: contract …`). This can take a while —
   ingestion is fast, but extraction now runs 10 sequential Cortex calls
   per contract, so several contracts at once is not a quick coffee
   break.
5. **Check the result.** Below the button, "Required contracts coverage"
   shows how many of the register's CW numbers have been extracted at
   least once and how many are current (no linked document has changed
   since); "Ingestion / indexing" shows raw counts and warns directly if
   `Documents ingested` > `Documents indexed` (some are still queued or
   failed — go to Data Sources → Index tab).
6. **Review on Contract Register** (see "Reviewing and verifying"
   below), then look the contract up on **Contract Lookup**.

### Workflow B — manual upload on Data Sources

1. *(First time only, or when the register changes)* **Data Sources →
   📋 Required Contracts Register tab** — upload the `.xlsx` and click
   **"Sync register."** This is what makes a CW number appear on
   Contract Lookup at all, independent of whether any document for it
   has been ingested yet.
2. **Data Sources → 📤 Upload Contract Files tab** — choose the PDF(s)
   (rarely DOCX) and click **"Ingest uploaded files."** Indexing runs
   automatically right after ingestion here — you'll see "Indexed N
   document(s)" in the result. A file with no "Signed"/"Executed" marker
   in its name or text is still ingested, just flagged so you can
   double-check it's the right copy.
3. **Extraction does NOT run automatically for this path.** Go to
   **Contract Register**: if the contract number doesn't already exist,
   use **"🔗 Link documents to a contract"** at the top to assign the
   uploaded document a CW number and a role (BASE/VARIATION/etc.), then
   click **"Link."** If the contract already exists and this is a new
   variation for it, link it the same way.
4. Still on **Contract Register**, find the contract (use "Filter to one
   contract" to jump straight to it) and click **"Run/refresh extraction
   for this contract."** This is also where you'd click **"Run
   extraction for all contracts"** instead, if you've just linked
   several contracts' worth of documents in one sitting.
5. **Review on Contract Register**, then look it up on **Contract
   Lookup**.

### Reviewing and verifying (Contract Register)

- **"Filter to one contract"** defaults to nothing selected — pick a CW
  number (or explicitly pick "All contracts") to see detail; with
  exactly one contract on screen, it renders directly, no expanding
  needed.
- Each field shows its extracted value, a confidence badge (🟢 High / 🟡
  Medium / 🟠 Low / 🔴 Not found), and **"View source"** to open the
  citation panel — the exact quoted passage plus, for PDFs, a rendered
  page with the passage highlighted and an "Open original document ↗"
  link.
- Toggle **"Show extracted fields as a table"** for a spreadsheet-style
  view of every field at once (useful for a fast pass over many fields;
  the boxed per-field view is easier for reading one section closely).
- Tick **"Verified"** on a field once you've checked it against the
  source — this is a human sign-off, not something extraction sets
  itself, and it's cleared automatically if that contract's documents
  change and extraction re-runs.
- Download buttons give four formats: **Word** and **PDF** (the full
  Contract Workspace Summary), and **Word (2-pg)** / **PDF (2-pg)** (the
  condensed version — one-to-two-sentence findings instead of full
  paragraphs, for a quick read or to hand to someone who doesn't need
  the full detail).

### Recovering from a stuck or partially-failed run

- **A run looks frozen mid-indexing** (no per-document progress line for
  several minutes): it's safe to close or refresh the tab — every
  ingest/index/extract step is a `MERGE`/hash-keyed upsert, not an
  append, so nothing is corrupted by walking away mid-run. **Don't**
  click "Check for new files now" again to resume — the inbox is
  already drained, so a fresh run finds no new files and skips indexing
  entirely, silently leaving anything unfinished stuck unindexed
  forever. Instead use **Data Sources → 🌳 Index tab → "Index
  new/unindexed documents"** — it indexes everything not yet in the
  index regardless of how it arrived, and reports live per-document
  progress.
- **A document shows up in "N document(s) failed to index"**: click
  **Data Sources → 🌳 Index tab → "Index new/unindexed documents"**
  again once you've redeployed a fix, or after any transient failure —
  this button is always safe to re-run, since `rebuild=False` means it
  only touches documents that aren't indexed yet, never re-doing
  successful ones.
- **After resolving either of the above, extraction still needs a
  separate trigger** — neither Data Sources index button runs
  extraction. Go to **Contract Register** and run it for the affected
  contract(s).
- **"Rebuild all"** (Data Sources → Index tab) re-indexes every
  document from scratch, including ones that already succeeded — only
  needed after a segmentation-profile/granularity change, never as a
  routine fix.

### Quick reference: every button, where it lives, and when to use it

| Page → tab | Button / control | What it does | When to use it |
|---|---|---|---|
| Data Sources → Required Contracts Register | Sync register | Adds CW numbers from an uploaded `.xlsx` to `CONTRACT_REGISTER` | The register workbook changes, or before a first bridge run for brand-new CW numbers |
| Data Sources → Upload Contract Files | Ingest uploaded files | Ingests + indexes the chosen PDF/DOCX file(s) immediately | You have a file on hand and don't want to wait for the bridge |
| Data Sources → Index | Index new/unindexed documents | Indexes every document not yet in `DOCUMENT_INDEX`, live progress | After any indexing failure or interrupted run; always safe to re-run |
| Data Sources → Index | Rebuild all | Re-indexes every document from scratch | Only after a segmentation profile/granularity change |
| Sync Status | Check for new files now | Runs the full bridge pipeline once (drain → ingest → index → extract), live log | Right after staging files via the bridge, instead of waiting 5 minutes |
| Contract Register | 🔗 Link documents to a contract | Assigns an unlinked document a CW number + role | After a manual upload, or to fix a wrongly-linked document (unlink first) |
| Contract Register | Filter to one contract | Narrows the page to one CW number (or "All contracts") | Always, once you know which contract you're reviewing — the default is nothing selected |
| Contract Register | Run/refresh extraction for this contract | Runs the 10-agent extraction for one contract, live progress | After linking new/changed documents to that contract |
| Contract Register | Run extraction for all contracts | Same, looped over every contract in the register | After a batch of manual uploads/links across several contracts |
| Contract Register | Show extracted fields as a table | Switches the per-field boxed view to a spreadsheet view | Reviewing many fields quickly, or exporting the confidence/verified columns visually |
| Contract Register | Verified checkbox | Human sign-off on one field | After checking a field's "View source" citation against the real document |
| Contract Register / Contract Lookup | ⬇ Word / PDF / Word (2-pg) / PDF (2-pg) | Downloads the Contract Workspace Summary in that format | Full formats for the record; 2-pg for a fast read or handoff |

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
  existed in the per-file inbox cleanup too — fixed there as well, even
  though it hadn't been exercised yet at the time, since it would have
  failed the same way. That fix immediately hit a third, more fundamental
  wall on the very next live run: `REMOVE` itself is confirmed
  **unsupported inside a Python stored procedure** ("Unsupported
  statement type 'REMOVE_FILES'") — no quoting fixes this, it's the same
  class of categorical restriction as `CREATE TEMPORARY TABLE` and `ALTER
  SESSION` above, just for a third statement type. `stage_pickup.py` was
  redesigned around this: instead of deleting a handled file from
  `NETWORK_DRIVE_INBOX_STAGE`, its path is recorded in a permanent
  `_STAGE_PICKUP_PROCESSED` table (created by
  `sql/04_stage_pickup_task.sql`), and `list_staged_files()`'s
  `DIRECTORY()` query left-joins against that table to exclude anything
  already handled — so a processed file is never reprocessed, it just
  stays physically present as harmless dead weight. Actually freeing that
  space requires `REMOVE`, which still works fine from *outside* a stored
  procedure (a worksheet, or a notebook cell) —
  `ingestion/stage_pickup.py`'s `purge_processed_inbox_files(session)`
  does exactly that and is meant to be run occasionally by hand, never
  automatically. One assumption remains genuinely unverified: `COPY FILES
  INTO <stage> FROM <stage>` (stage-to-stage, used in
  `ingestion/stage_pickup.py` to avoid a GET/PUT round trip) behaves as
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
- The Sync Status page's "Check for new files now" button calls
  `ingestion.stage_pickup.run_stage_pickup()` directly in-process (same
  pattern `ingestion.file_ingest.ingest_uploaded_files()`'s Streamlit
  callers already use) instead of `CALL RUN_LEX_STAGE_PICKUP()`, so an
  `on_progress` callback can show live per-file/per-contract status
  instead of one opaque spinner for the whole run. The scheduled Task
  still goes through the stored procedure, unaffected — both paths call
  the identical function underneath, `on_progress` is just `None` there.

  CONFIRMED on a live account: `run_stage_pickup`'s own call to
  `index_builder.build_index_for_project` was missing `on_progress`
  entirely, even though that function fully supports it and reports one
  message per document (`[i/n] filename — indexing…`). A 27-document
  ingest left the Sync Status log frozen on "Indexing N new/updated
  document(s)…" for over an hour with no further updates — not
  necessarily because indexing was stuck, but because nothing from that
  phase was ever reaching the page regardless of how long it genuinely
  took. Fixed by threading `on_progress` through. If a run is ever
  genuinely stuck again with this fix deployed (no per-document line
  appears for several minutes), it's safe to close/refresh the tab —
  every ingest/index/extract step in this pipeline is a MERGE or
  hash-keyed upsert, not an append, so nothing is corrupted by walking
  away mid-run. To resume cleanly afterward: use **Data Sources → 🌳
  Index tab → "Index new/unindexed documents"**, not "Check for new
  files now" again — the inbox files are already drained by that point,
  so a fresh stage-pickup run would find nothing new and skip indexing
  entirely (`new_doc_ids` would be empty), silently leaving any
  documents that didn't finish indexing before the interruption
  unindexed. The Data Sources button indexes every document not yet in
  `DOCUMENT_INDEX` regardless of how it got ingested, and already has
  `on_progress` wired correctly. Extraction still needs a separate
  manual trigger afterward (Contract Register, per-contract or "Run
  extraction for all contracts") — the Data Sources indexing button
  doesn't run it.
- **Stock-field extraction reads full documents directly, not narrow
  retrieval.** CONFIRMED on a live account: the original design (one
  `query_engine.search()` call per field, scoped to a contract's family
  via `restrict_to_doc_ids`) kept returning "the excerpts provided do not
  contain sufficient information" for most fields on real, densely-written
  contracts — that retrieval/reranking pipeline wasn't reliably surfacing
  the right section for a given question even when the answer was
  genuinely in the documents. `extract_stock_fields_for_contract` now
  runs one "agent" per small cluster of 2-3 closely related fields
  (`FIELD_GROUPS` — 10 clusters covering the 21 stock fields, e.g.
  "Novation & confidentiality", "Pricing & labour"), each a single Cortex
  call that reads **every** one of the contract's linked documents in
  full (base + every variation/extension/novation, oldest first),
  explicitly instructed to narrate how a fact evolved across documents
  rather than just stating the latest value. `query_engine.py` is no
  longer called by anything in the live app as a result — kept as
  groundwork for a possible future free-form chat feature, per its own
  and `Chat.py`'s docstrings.

  CONFIRMED on a live account: an earlier version of this ran one agent
  per whole template section (3 calls of ~5-8 questions each, not 10 of
  2-3) and produced noticeably more LOW-confidence fields than the
  10-cluster version — cramming that many questions into one call over a
  large multi-document context measurably hurt the model's ability to
  correctly name which document a genuine verbatim quote came from.
  `_extract_field_group` now also double-checks a quote against every
  document in the family before giving up on it, not just the one
  document number the model claimed — a real quote misattributed to the
  wrong document was previously discarded as "unverified" and dragged
  confidence down to LOW for an answer that was actually well-grounded.
  These agents still run one at a time, not concurrently — Snowpark's
  session isn't documented as safe for concurrent statement execution
  across threads, and this app only has the one session
  Streamlit-in-Snowflake hands it, so there's no straightforward way to
  parallelize across independent Snowflake connections from inside it.
  Ten sequential full-family reads costs roughly 3x the input tokens and
  wall time of the earlier 3-agent version per extraction run.

  The extraction prompt also now asks the model to end a finding with a
  one-sentence practical risk assessment (e.g. "Assessment: consent
  required; medium risk for ownership changes") for judgement-style
  questions, matching the reviewer-style framing of the Microsoft
  CoPilot-generated reference summary this was benchmarked against for
  CW20841 — and `generate_classification_scorecard`'s prompt now
  explicitly tells the model to be decisive (e.g. rate `OVERALL_CLASSIFICATION`
  as High when the findings describe safety-critical/24-7/high-value
  operations) rather than defaulting to a hedged "Moderate" rating that
  can end up inconsistent with what the detailed findings actually say.

  CONFIRMED on a live account (comparing a second CW20841 run against
  CoPilot's own detailed reference output, which rates risk consistently
  from the contracting client's side): the risk-assessment instruction's
  original "risk...for the party asking the question" wording was
  ambiguous enough that several fields (novation, auto-renewal, change of
  control) got framed as risk *to the supplier* instead — a restriction
  on the supplier's ability to disclose information read as "bad for the
  supplier" rather than "protective of the client," which isn't the
  perspective this register is kept from. The instruction now says
  explicitly: risk to the client who engaged the supplier named in the
  SUPPLIER field, never the supplier's own risk. It also now requires
  committing to exactly one of Low/Medium/High rather than a hedge like
  "moderate-to-high" (COMPLEXITY's question asks for this rating
  explicitly too, since it isn't phrased as a "risk" question and so
  wasn't reliably triggering the generic risk-sentence instruction on its
  own).

  Document ordering (oldest → newest, so the model can narrate the
  evolution correctly) uses the same `EFFECTIVE_DATE` → `SEQUENCE_NO` →
  filename precedence as `contract_linking.list_family_documents` —
  `EFFECTIVE_DATE` (the most trustworthy signal) isn't populated by
  anything in this codebase today, so in practice this falls back to
  `SEQUENCE_NO`, which `ingestion/stage_pickup.py` sets to the order
  files for a contract were processed *in one pickup run* — a real but
  weaker signal (processing order, not necessarily true chronology, if
  the same contract's files are staged across separate runs out of
  order). Setting a real `EFFECTIVE_DATE` (once some caller actually
  does) immediately takes priority with no code change needed.

  A family's combined document text is capped at
  `_FULL_TEXT_BUDGET_CHARS` (200,000 characters) per agent call — the
  newest documents are kept whole and the oldest truncated/dropped first
  if a family's total exceeds it, matching the same recency-wins
  precedence. UNVERIFIED: this ceiling is a conservative guess, not a
  confirmed model context-window limit on this account.
- `citation_viewer.get_presigned_url()` returns `(url, error)` instead of
  just `url` — a `None` url with a swallowed exception gave "Couldn't
  generate a link to the original document" with no way to tell why
  (missing stage privileges on the viewing role vs. a genuinely missing
  file vs. something else); the real error now surfaces in
  `citation_panel_ui.py`'s warning text. That panel also now always shows
  a real `st.link_button` ("Open original document ↗") when a URL exists,
  not just an anchor inside the embedded PDF.js iframe — that one silently
  does nothing if the CDN script never loads (see `citation_viewer.py`'s
  own docstring on that being unverified from this environment), so a
  Streamlit-native link outside the iframe is the one that's guaranteed to
  actually open in a new tab.

  CONFIRMED on a live account: one specific document's citation link
  stayed broken ("Argument 2 to function 'GET_PRESIGNED_URL' cannot be
  null or empty") across repeated re-extractions even after the
  empty-relative-path guard above — the real root cause was
  `SQLBuilder.build_merge_raw_document_by_source_item`'s `WHEN MATCHED`
  clause only refreshing `FILE_NAME`/`STAGE_PATH`/`SOURCE_TYPE` alongside
  `RAW_TEXT`/`SOURCE_HASH` when a document's parsed content had changed.
  This project's stage got dropped/recreated and files re-copied more
  than once during earlier debugging; a document's `STAGE_PATH` baked in
  under an earlier, since-fixed ingestion bug was never revisited by a
  later, correct run once its `SOURCE_HASH` stopped changing. Fixed with
  a second, unconditional `WHEN MATCHED` clause that always refreshes
  where a file currently lives, independent of whether its content also
  changed — a file's stage location this run isn't something that should
  ever be conditional on its content, unlike `RAW_TEXT`/`SOURCE_HASH`/
  `PARSED_AT`, which stay conditional (re-parsing/re-indexing is the
  actually expensive, worth-avoiding part).
- Only ONE citation is ever recorded per extracted field
  (`CONTRACT_FIELD_EXTRACTS.SOURCE_DOC_ID`/`SOURCE_NODE_ID`/`SOURCE_QUOTE`
  are single columns, not a list) — `_extract_field_group` records
  whichever single document the model named as most directly supporting
  the current answer, even when a field's value narrates how it evolved
  across several documents (e.g. "originally $X [Document 1], increased
  to $Y [Document 3]"). `SOURCE_NODE_ID` is always `NULL` now too — there's
  no `DOCUMENT_INDEX` section involved once extraction reads full
  documents directly instead of retrieved sections. The Contract
  Register's tabular view (`_render_fields_table`, toggled via **"Show
  extracted fields as a table"**) reflects this honestly: one "Source"
  link per row, not a link per document the value text narrates. Tracking
  every contributing document, not just one, would need a schema change
  (a `CITED_DOCS` VARIANT column or similar) — not done here.
- **Every extracted field carries a citation unless it's genuinely
  `NOT_FOUND`** — `SOURCE_DOC_ID` is set whenever the model named a
  source at all, independent of whether a verbatim quote also verified.
  `_confidence_for_full_text` used to collapse "named a source but no
  single sentence verified word-for-word" into the same `LOW` bucket as
  "the model didn't name a source at all" — misleading specifically for
  this architecture, where a field's value routinely synthesizes a fact
  across several documents (e.g. "originally $X [Document 1], increased
  to $Y [Document 3]") and so has no ONE sentence that captures the whole
  synthesized answer, even though it's well-grounded. Split into a
  `MEDIUM` tier for exactly that case; `LOW` is now reserved for the
  genuinely rare case of a real answer with no named source at all.
  `citation_panel_ui.render_citation_panel` was also silently showing
  nothing for a `MEDIUM` field (gated on `SOURCE_QUOTE` alone) — it now
  shows the source document name and an "Open original document" link
  even without a highlightable quote, with an honest note that no single
  passage could be verified word-for-word.
- Contract Register's **"Filter to one contract"** dropdown existed
  before this round but only narrowed which contract(s) still rendered
  inside their own collapsible `st.expander` — functionally a filter, but
  it kept the "click to open" framing of a multi-contract list even once
  only one contract was left to show. When exactly one contract is on
  screen (via that filter, or because only one is linked at all), it now
  renders as a plain always-visible section instead — no expander, no
  click needed. Rendering multiple contracts at once (the default "All
  contracts" state) is unchanged. `Chat.py` and this page's single-
  contract view also now show the Key Commercial Risks / Procurement
  Recommendation / Retender Strategy fields live, not just in the
  downloaded Word/PDF — an oversight from when those fields were added
  (`docx_report.py`/`pdf_report.py` got them immediately; the live
  Streamlit views didn't). "Run extraction for all contracts" now reports
  live per-contract, per-agent progress the same way the single-contract
  button already did (`extract_stock_fields_for_all_contracts`'s new
  `on_progress` parameter, prefixing each inner agent message with its
  own contract's CW number) — it used to be one plain spinner for the
  whole multi-contract run.
- Indexing (`ingestion/index_builder.py`) can fail with `Cortex response
  was not valid JSON: Unterminated string...` — CONFIRMED on a live
  account for 3 dense contract documents under
  `SEGMENTATION_GRANULARITY='DETAILED'`, and it's an *output* problem, not
  an input one: the model's JSON listing every section it found got cut
  off before finishing, at exactly `utils/cortex_client.py`'s
  `MAX_JSON_RETRY_TOKENS` ceiling (raised from 16000 to 24000, but not
  verified against whatever hard output-token ceiling the underlying
  model itself may have — if the same error recurs at the new ceiling,
  that's almost certainly it, and no further raise will help).
  `PROJECTS.MAX_DOCUMENT_CHARS` (the per-chunk size fed to each indexing
  call) is the other lever, lowered from 100000 to 60000 in both the
  schema default and the provisioning notebook's own explicit value
  (which otherwise re-applies 100000 on every re-run of the "Create the
  LEX project" cell, silently undoing a one-off `UPDATE`) — a smaller
  chunk means fewer sections found per call, and thus a shorter response,
  independent of whatever the model's true output ceiling turns out to
  be. Lower it further (a direct `UPDATE PROJECTS SET MAX_DOCUMENT_CHARS
  = ... WHERE PROJECT_CODE = 'LEX'` takes effect immediately, no redeploy)
  if the error still recurs — then re-run **Data Sources → Index →
  Rebuild all** for whichever documents failed.

  CONFIRMED on a live account: raising `MAX_JSON_RETRY_TOKENS` and
  lowering the chunk size wasn't enough for every document — a handful
  ingested via a later `lex_network_bridge` batch (including a rolling-
  stock spare-parts catalogue "listing thousands of individual parts,
  components and repair" items) still truncated even a single 60,000-
  character chunk's segmentation response, because `DETAILED`
  granularity's one-section-per-sub-clause instruction has no natural
  stopping point against genuinely list-shaped content — there's no
  chunk size small enough to fix that without also shrinking chunks for
  every normal document. Rather than chase the ceiling further,
  `_index_one_document` now degrades gracefully per chunk instead of
  failing the whole document: when a chunk's segmentation call still
  fails, it falls back to one plain-prose `complete()` summary call for
  just that chunk (a far smaller ask that in practice doesn't hit this
  failure mode), contributing zero sections from that chunk but not
  losing the rest of the document's index or its
  `CONTRACT_REGISTER`-visible document-level summary. This is a
  reasonable trade specifically because fine-grained sections aren't
  read by anything in the live app today (`query_engine.search()` is
  dormant — see that module's own docstring); only the document-level
  summary is, via `contract_linking.get_significant_variations`'s
  Significant Variations display. A document that previously showed up
  in "N document(s) failed to index" for this exact reason should now
  index successfully (with a plainer document-level summary and fewer
  fine-grained sections than a cleanly-structured contract would get) —
  re-run **Data Sources → Index → "Index new/unindexed documents"** to
  pick up anything still marked failed from before this fix.
- A DIFFERENT, unrelated failure — `"Parsed text too short"` — happens at
  ingestion, before a document even reaches indexing: `AI_PARSE_DOCUMENT`
  produced under `config.MIN_PARSED_TEXT_CHARS` (100) characters of OCR
  text for that file. CONFIRMED on a live account as a genuinely
  persistent failure for one specific document across many separate
  stage-pickup runs, not a transient one — `_mark_processed` deliberately
  never marks a `FAILED` file as processed (see that function's own
  docstring), so it's retried on every future run until it's fixed or
  replaced on the network drive, which also means a human staring at the
  Nth identical retry learns nothing new from the bare message alone. The
  error now includes the actual character count (`"Parsed text too short
  (N char(s), need 100)"`) — a handful of characters points at a
  corrupt/blank/image-unreadable PDF worth opening directly to check,
  while a count just under 100 points at `MIN_PARSED_TEXT_CHARS` itself
  being stricter than a genuinely short but valid document (e.g. a
  one-page amendment) needs. It now also shows the actual extracted text
  itself — `"— no visible text extracted at all"` when OCR found literal
  whitespace only (the clearest sign of a blank scanned page), or
  `"— all OCR found: '...'"` with up to 200 characters of whatever OCR did
  produce otherwise — so a low character count that turns out to be
  genuine garbage/noise (a corrupt or unreadable scan) is visibly
  different from a low count that's a real short fragment, all from the
  Sync Status banner, with no need to open the file or query Snowflake
  directly to tell which one you're looking at.

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
