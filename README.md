# LEX — Legal EXtraction & Contract Intelligence

**LEX** is a Streamlit-in-Snowflake chatbot, backed by Snowflake Cortex AI,
for the MR5 Transition Contracts Team. It's built on `project-llm-wiki`, the
same reusable multi-project LLM Wiki engine that runs
[`ORG_MM_CHAT`](https://github.com/ds-madhavan-ramani/org_mm_chat) in
production — LEX is the second real project forked from it, extended with a
contract-specific segmentation profile and an automated stock-question
extraction layer neither the original template nor `ORG_MM_CHAT` needed.

## What it does

- **Source of truth**: Signed & Executed Contracts held on the team's
  SharePoint / network-drive folder (a service-account-accessible Microsoft
  Graph API source — confirm this is genuinely SharePoint and not a raw
  file share before Build phase; see "Open items" below).
- **Ingestion**: pulled from SharePoint via the Microsoft Graph API, or
  uploaded directly, on demand from the app's Data Sources page.
- **Retrieval**: not vector-chunked RAG. Each document is indexed into a
  navigable tree (document → clause/schedule section, with LLM-generated
  summaries), and questions are answered by having the model traverse that
  tree — answers cite the exact source document, with hybrid vector search
  layered on top since a 600-contract corpus needs more than summary-based
  routing alone.
- **The 15 stock questions**: contract title, CW number, end date,
  one-sentence summary, novation consent, disclosure clause, extension
  options, complexity of goods/services, separable portions, payment
  regime, securities, price review mechanism, EA clauses, termination
  clause, and auto-renewal mechanism — answered automatically for every
  contract (and kept current as variations/extensions are linked in), each
  with a citation and a confidence badge, on the **Contract Register**
  page.
- **Contract families**: a signed contract is rarely one static document —
  its variations, extensions, and novation deeds are ingested as their own
  documents, then linked together on the Contract Register page so both
  chat and the stock-question extraction consider a contract's full
  history, not just its base agreement.
- **Chat UI**: free-form follow-up questions, with cited sources shown
  under every answer and a cache layer so repeated questions don't re-run
  the full search. No conversation memory yet (each question is answered
  independently) — a deliberate v1 scope decision, not a gap; see the
  Delivery Plan.

## How it works

```
SharePoint / network drive (Signed & Executed Contracts)
        │  Microsoft Graph API
        ▼
RAW_DOCUMENTS  (LEXDB.DATA_LEX)
   .xlsx → parsed natively (stdlib zipfile/XML, no third-party package)
   .pdf/.docx/.txt → AI_PARSE_DOCUMENT (OCR)
        │  index_builder.py (LEX_CONTRACT segmentation profile,
        │  chunked for documents beyond MAX_DOCUMENT_CHARS — a 500-page
        │  contract does not fit one indexing call)
        ▼
DOCUMENT_INDEX  — clause/schedule-level tree, each section with an LLM
                  summary + (optionally) a semantic embedding
        │
        ├─ query_engine.py — tree search (doc → section routing, keyword
        │  fallback, hybrid vector search, reranking), via Cortex
        │  AI_COMPLETE — this IS the free-form Chat engine
        │
        └─ contract_extraction.py — the same query_engine.search(), called
           once per stock question per contract family (via
           restrict_to_doc_ids), persisted with citation + confidence into
           CONTRACT_FIELD_EXTRACTS for the Contract Register page
```

`CONTRACT_REGISTER` + `CONTRACT_DOCUMENT_LINK` (which documents belong to
which real-world contract, and in what role — base / variation / extension
/ novation / deed of amendment) sit alongside `RAW_DOCUMENTS` /
`DOCUMENT_INDEX` in the same schema; see `sql/03_lex_contract_tables.sql`.

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

Run `sql/test_graph_connectivity.sql` to confirm all three exist before
deploying.

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

Open the app: Snowsight → **Streamlit** → `LEX_APP`.

## Using the app

- **Chat** — the default/landing page. Ask a question, get a cited answer.
  No memory between questions yet.
- **Data Sources** — upload files directly, or list and select from the
  configured SharePoint/network-drive folder. Newly ingested or updated
  documents are indexed automatically.
- **Contract Register** — link a contract's documents (base + variations/
  extensions/novations) into one family, then run the 15 stock questions
  for it. Every field shows its citation and a confidence badge; tick
  **Verified** once a human has checked it. "Not found in the documents"
  is treated as a valid, honest answer — never a guess.
- **Sync Status** — document/index counts and recent ingestion run history.

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
| `LEX_CONTRACT` segmentation profile (`index_builder.py`) | Clause/schedule-aware sectioning for legal contracts, not generic prose or meeting-minutes sectioning |
| Chunked indexing (`index_builder.py`) | A 100–500 page contract doesn't fit in one indexing call the way a few pages of meeting minutes does — see the function's own docstring |
| `query_engine.search()`'s `restrict_to_doc_ids` parameter | Lets a caller scope search to one contract's linked documents — what makes the stock-field extraction just "chat with the document set pre-selected" |
| `CONTRACT_REGISTER` / `CONTRACT_DOCUMENT_LINK` / `CONTRACT_FIELD_EXTRACTS` + `contract_linking.py` / `contract_extraction.py` | Contract-family linking and the automated, persisted, cited 15-stock-field extraction — nothing like this exists in the generic template |
| **Contract Register** Streamlit page | New UI surface — nothing like it in `org_mm_chat` |

## Open items

Carried over from the Delivery Plan's open questions — resolve these
before Build phase runs on real signed contracts:

1. Confirm the "network drive" is actually a SharePoint library reachable
   via Graph API (assumed throughout this repo) — a raw file share needs a
   different ingestion bridge.
2. The 3–5 named users for the `LEX_USERS` role.
3. Whatever identifies the BG/Cash securities-reconciliation list, for a
   future `SECURITIES_RECONCILIATION` view.
4. The existing CW-number formatting convention, so
   `contract_linking.suggest_cw_number()`'s auto-suggestion is reliable.
5. Confirmation that Cortex cross-region inference (to AWS AU) is
   acceptable for signed contract content, from a data-handling/compliance
   standpoint.

## Further reading

- **[LEX_Delivery_Plan.html](./LEX_Delivery_Plan.html)** ([published version](https://claude.ai/code/artifact/62475db9-d82d-41a0-b66e-1f85f2efbf4d))
  — full scope, architecture rationale, security/access design, and the
  phased Build (8 contracts) → Scale (600 contracts) execution roadmap.
- **[LEX_Solution_Architecture.html](./LEX_Solution_Architecture.html)** ([published version](https://claude.ai/code/artifact/ef0664c5-268e-42b8-ab4e-553e9cbb785d))
  — the contextual block-diagram architecture: users, Streamlit-in-
  Snowflake, the security perimeter, and cross-region Cortex inference.
