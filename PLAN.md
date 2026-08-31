# LEX — Legal EXtraction & Contract Intelligence

**Sponsor / team:** MR5 Transition Contracts Team
**Status:** Proposal — Build phase not yet started
**Base platform:** the `project-llm-wiki` engine (the same engine
`ORG_MM_CHAT` runs on), as it exists today in
[`ds-madhavan-ramani/org_mm_chat`](https://github.com/ds-madhavan-ramani/org_mm_chat).
LEX is a **second project instance** of that template. Every code path
named below (`sql/...`, `python/...`, `streamlit/...`) refers to that
repo — see this repo's `README.md` "Relationship to project-llm-wiki" for
what Phase 0 needs to copy/adapt into *this* repo.

---

## 1. Objective

Give the MR5 Transition Contracts Team a Streamlit chat tool, backed by
Snowflake Cortex AI and a RAG retrieval engine, that:

1. Answers a fixed set of **16 stock questions** for every contract
   automatically, with citations, the moment a contract (and its
   variations/extensions) are ingested.
2. Lets users ask **free-form follow-up questions** against the same
   contract corpus, answered concisely, in a professional tone, always
   with a citation back to the source document (URL where available).
3. Starts small (**8 contracts, Build phase**) and scales to **~600
   contracts, 100–500 pages each** (Scale phase) without a redesign.
4. Runs in an **isolated ComputePool + Database + Schema**, restricted to
   3–5 named users.
5. Has **no conversational memory** at launch (each question is
   independent) — called out explicitly as a v2 candidate, not a gap.

Non-goals for v1: contract drafting/redlining, obligation-tracking
workflow, e-signature integration, or write-back to the source library.

---

## 2. What's reused vs. what's new

`project-llm-wiki` already solves most of the hard infrastructure problems
(catalog-driven multi-project isolation, SharePoint ingestion via Graph
API, hierarchical tree-search RAG, citation plumbing, Streamlit-in-Snowflake
deployment quirks). LEX should provision as a **new row in
`MEDSOCMS.APP_CATALOG.PROJECTS`** (`PROJECT_CODE = 'LEX'`), the same way
`ORG_MM_CHAT` did, and reuse:

| Reused as-is | Notes |
|---|---|
| `CREATE_PROJECT` / `TEARDOWN_PROJECT` procs | Provisioning pattern (§9 covers the one schema change needed) |
| `python/utils/graph_client.py` | Generic Graph API folder client — already source-agnostic |
| `python/ingestion/file_ingest.py` / `AI_PARSE_DOCUMENT` OCR path | For PDF/DOCX contracts |
| `python/query_engine.py` tree search (doc → section routing, keyword fallback, reranking, vector search) | This **is** the free-form Q&A engine (§6) |
| Citation numbering + `SOURCE_URL` plumbing | Already produces clickable citations from SharePoint `webUrl` |
| Streamlit-in-Snowflake deploy pattern (flattened stage, `SECRETS` clause, container-runtime lessons in the `org_mm_chat` README's "Known account-level gotchas") | Directly applicable — avoids re-discovering the same bugs |

New for LEX (detailed below):

| New | Why existing template doesn't cover it |
|---|---|
| `LEX_CONTRACT` segmentation profile | Contracts need clause/schedule-aware sectioning, not meeting-minutes sectioning |
| Contract-family linking (base + variations/extensions/novations) | Template has no concept of one logical contract spanning multiple documents |
| `CONTRACT_FIELD_EXTRACTS` table + auto-extraction job | Template only does ad-hoc Q&A; it has no "run these 16 questions automatically and persist the answers" concept |
| Paginated/chunked indexing for 100–500 page documents | `MAX_DOCUMENT_CHARS` truncation (see §5.3 — this is a real gap, not a tuning knob) |
| Per-project dedicated **database** (not just schema) | Template's `CREATE_PROJECT` hardcodes `MEDSOCMS`; user requirement is DB+schema+pool isolation |
| Named-user access role | Template has no per-project RBAC row; every `ORG_MM_CHAT`-style project currently just inherits `ADVANCEDANALYTICS` |
| Contract Register / Field Review UI page | New Streamlit page — nothing like it exists today |

---

## 3. Source of truth & ingestion

The requirements describe the source two ways: "a specific Network Drive
... using a Service Account Credential" and, separately, "connectivity to
SharePoint using Service Account and with the MS Graph API Client."
**These are reconciled as the same thing**: Snowflake cannot mount an SMB/
network file share directly (no native connector), and the existing
template's only outbound document connector is Microsoft Graph. The
practical read is that the "network drive" is a SharePoint document
library the team accesses today via a synced/mapped drive letter
(OneDrive/SharePoint sync client) — the same pattern `ORG_MM_CHAT` already
uses for `cabinet-mr4`. **Confirm this with the team before Build phase**;
if it is genuinely a raw file-server SMB share with no SharePoint/OneDrive
layer, ingestion needs a different bridge (e.g., a scheduled agent that
copies files to a Snowflake stage via `PUT`, since Graph API cannot reach
it) — flagged as an open question in §13, not assumed away.

Ingestion path (reusing the template's existing mechanism):

```
SharePoint library (Signed & Executed Contracts)
   │  Microsoft Graph API, client-credentials flow
   │  Service Account = dedicated Azure AD app registration,
   │  Sites.Selected permission scoped to ONLY this library
   ▼
Streamlit "Data Sources" page → list files → tick → Ingest
   │  AI_PARSE_DOCUMENT (OCR) for PDF/DOCX; native parse for DOCX text layer
   ▼
RAW_DOCUMENTS  (dedup on SharePoint item ID; edited files update in place)
```

A dedicated app registration (own Client ID, own secret) is recommended
rather than reusing `ORG_MM_CHAT`'s tenant-level Graph app, scoped via
`Sites.Selected` to *only* the contracts library — least-privilege, and
means revoking LEX's access later doesn't touch other projects.

---

## 4. Data model

### 4.1 Catalog row (`MEDSOCMS.APP_CATALOG.PROJECTS`)

One new row, `PROJECT_CODE = 'LEX'`, `SEGMENTATION_PROFILE = 'LEX_CONTRACT'`,
`COMPUTE_POOL = 'LEX_COMPUTE_POOL'` (container runtime — needed anyway for
a modern pinned Streamlit version, see the `org_mm_chat` README's
account-level gotchas), `ENABLE_VECTOR_SEARCH = TRUE` and
`ENABLE_RERANKING = TRUE` from day one (600 documents, each up to 500
pages, makes document-summary-only routing too coarse — hybrid retrieval
is not optional at that scale the way it is for ~90 short meeting
minutes).

### 4.2 Per-project schema (`DATA_LEX`)

Reused unchanged:

- `RAW_DOCUMENTS` — one row per physical document (base contract, each
  variation, each extension, each novation deed are all **separate rows**).
- `DOCUMENT_INDEX` — hierarchical tree, per document (see §5 for the
  contract-specific profile and the large-document chunking change).

New tables:

```sql
-- One row per logical contract (the CW number is the real-world identity;
-- a contract can span many RAW_DOCUMENTS over its life).
CREATE TABLE CONTRACT_REGISTER (
    CONTRACT_ID       INT IDENTITY PRIMARY KEY,
    CW_NUMBER          VARCHAR(50) NOT NULL UNIQUE,
    CONTRACT_TITLE      VARCHAR(500),
    STATUS               VARCHAR(20) DEFAULT 'ACTIVE',  -- ACTIVE | EXPIRED | SUPERSEDED
    CREATED_AT            TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);

-- Which physical documents make up a contract's history, and their role.
CREATE TABLE CONTRACT_DOCUMENT_LINK (
    LINK_ID          INT IDENTITY PRIMARY KEY,
    CONTRACT_ID       INT NOT NULL REFERENCES CONTRACT_REGISTER(CONTRACT_ID),
    DOC_ID             INT NOT NULL REFERENCES RAW_DOCUMENTS(DOC_ID),
    DOC_ROLE            VARCHAR(20) NOT NULL,  -- BASE | VARIATION | EXTENSION | NOVATION | DEED_OF_AMENDMENT
    EFFECTIVE_DATE       DATE,
    SEQUENCE_NO           INT,                  -- ordering within the family
    LINKED_BY              VARCHAR(200),          -- 'AUTO' (CW-number match) or a user
    LINKED_AT               TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);

-- One row per (contract, stock field) — the persisted, citable answer to
-- each of the 16 stock questions, kept current as new documents land.
CREATE TABLE CONTRACT_FIELD_EXTRACTS (
    EXTRACT_ID        INT IDENTITY PRIMARY KEY,
    CONTRACT_ID        INT NOT NULL REFERENCES CONTRACT_REGISTER(CONTRACT_ID),
    FIELD_KEY           VARCHAR(50) NOT NULL,   -- see §6.1 for the fixed key list
    FIELD_VALUE           VARCHAR(4000),          -- the concise answer
    SOURCE_DOC_ID           INT REFERENCES RAW_DOCUMENTS(DOC_ID),  -- which document in the family it was drawn from
    SOURCE_NODE_ID           INT REFERENCES DOCUMENT_INDEX(NODE_ID), -- supporting section
    SOURCE_QUOTE               VARCHAR(2000),          -- verbatim clause text, for audit
    CONFIDENCE                   VARCHAR(20),             -- HIGH | MEDIUM | LOW | NOT_FOUND
    MODEL_USED                    VARCHAR(50),
    EXTRACTED_AT                   TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
    IS_VERIFIED                     BOOLEAN DEFAULT FALSE,   -- human sign-off
    VERIFIED_BY                      VARCHAR(200),
    VERIFIED_AT                       TIMESTAMP_NTZ,
    UNIQUE (CONTRACT_ID, FIELD_KEY)
);
```

`CONTRACT_DOCUMENT_LINK` can auto-populate from CW number matching at
ingest time (if the contract title/CW number is extracted consistently)
but should always be **user-confirmable** in the UI — a wrong auto-link
between two unrelated contracts that happen to share a number format is a
correctness risk in a legal tool.

---

## 5. Ingestion & indexing for contract documents

### 5.1 New segmentation profile: `LEX_CONTRACT`

Add a `PROMPTS['LEX_CONTRACT']` entry to `index_builder.py`, parallel to
`ORG_MEETING_MINUTES`. Instructed to segment by clause/schedule/annexure
structure rather than generic prose breaks, and to quote defined terms,
clause numbers, dollar figures, and dates verbatim (the same "don't
paraphrase identifiers" lesson the ORG profile already encodes, applied to
clause numbers instead of action IDs).

### 5.2 Handling variations/extensions/novations

Each amending document is ingested and indexed as its own `RAW_DOCUMENTS`
row (own tree in `DOCUMENT_INDEX`), then linked into
`CONTRACT_DOCUMENT_LINK` under its parent contract. Free-form Q&A can
route across the whole family (all `DOC_ID`s under one `CONTRACT_ID`) so a
question like "what's the current end date" naturally considers the most
recent extension, not just the base contract. The 16 stock fields'
extraction job (§6) always re-resolves after any new document lands in a
family — a variation to termination clauses shouldn't leave a stale answer
sitting in `CONTRACT_FIELD_EXTRACTS` from the base contract.

### 5.3 The 100–500 page problem — a real gap, not a tuning knob

`index_builder._index_one_document` currently does `raw_text[:
project.max_document_chars]` (default 150,000 chars) before a **single**
LLM call builds the whole section tree. 150k characters is roughly 25–30k
words — around 50 pages. **A 500-page contract would silently lose
everything past that point from the index today.** This must be fixed
before Build phase validates against real contracts, not deferred to Scale
phase:

- Chunk long documents (e.g. every ~40–60 pages, using
  `AI_PARSE_DOCUMENT`'s page boundaries) and run the segmentation prompt
  per chunk.
- Insert one `'document'`-level node per document as today, but allow
  multiple chunks' sections to attach under it (an intermediate
  `'chunk'` `NODE_LEVEL` is a clean option, or simply insert all chunks'
  sections as siblings under the same root — either works with the
  existing `NODE_LEVEL` enum, the former is more scalable if
  `MAX_CANDIDATE_SECTIONS` needs raising later).
- `RAW_DOCUMENTS.RAW_TEXT` (`VARCHAR(16777216)`, i.e. 16MB) already holds
  the *full* extracted text without truncation — the fix is entirely in
  the indexing pass, not storage.
- This directly affects retrieval quality for every one of the 8 build
  contracts if any are already near/above 50 pages — worth validating on
  day one, not discovered during the scale-up.

---

## 6. The 16 stock questions

### 6.1 Fixed field list (`CONTRACT_FIELD_EXTRACTS.FIELD_KEY`)

| Key | Question |
|---|---|
| `CONTRACT_TITLE` | Contract title |
| `CW_NUMBER` | CW number |
| `CONTRACT_END_DATE` | Contract end date |
| `CONTRACT_SUMMARY` | One-sentence summary |
| `NOVATION_CONSENT` | Novation clause — is consent required? |
| `DISCLOSURE_CLAUSE` | Disclosure clause |
| `EXTENSION_OPTIONS` | Extension options |
| `COMPLEXITY_GOODS_SERVICES` | Complexity of goods/services |
| `SEPARABLE_PORTIONS` | Separable portions |
| `PAYMENT_REGIME` | Payment regime — claims process or milestone payments |
| `SECURITIES` | Securities (for reconciliation against the BG/Cash list) |
| `PRICE_REVIEW_MECHANISM` | Price review mechanisms |
| `EA_CLAUSES` | EA clauses |
| `TERMINATION_CLAUSE` | Termination clauses, notice period, perpetual-contract restrictions |
| `AUTO_RENEWAL_MECHANISM` | Auto-renewal mechanism, and whether a change of ownership/renewal cycle triggers a right to renegotiate T&Cs |

### 6.2 How the answers get generated

Rather than a bespoke single-shot "dump the whole contract and ask 16
questions at once" prompt (which the §5.3 page-count problem rules out
anyway), **each stock question is just a canned call into the same
`query_engine.search()` used for free-form chat**, scoped to one
contract's document family:

```
For each CONTRACT_ID, for each of the 16 FIELD_KEYs:
    answer = query_engine.search(session, project, question_text,
                                  restrict_to_doc_ids=family_doc_ids)
    UPSERT CONTRACT_FIELD_EXTRACTS
        (CONTRACT_ID, FIELD_KEY, FIELD_VALUE=answer.text,
         SOURCE_DOC_ID=answer.top_citation.doc_id,
         SOURCE_NODE_ID=answer.top_citation.node_id,
         SOURCE_QUOTE=answer.top_citation.excerpt, ...)
```

This has three advantages: it's the *same* retrieval/citation/synthesis
code path already built and hardened for chat (no parallel extraction
engine to maintain), it naturally handles the family-wide routing from
§5.2, and it degrades the same way chat does — "not found in the
documents" rather than a hallucinated field. `query_engine.search` needs
one small extension: an optional document-ID allowlist parameter (today
routing is always project-wide), plus a `max_tokens`/style instruction for
this batch mode (shorter, more clipped answers than a conversational
reply).

Runs automatically: (a) once when a contract family is first fully
ingested, (b) again whenever a new document lands in an existing family
(§5.2), (c) on demand from the Contract Register UI ("Re-run extraction").
For 8 contracts this is instant; for 600 contracts this is a scheduled
Snowflake **Task** (batch job on the warehouse), not something triggered
inline from a user's Streamlit click.

### 6.3 Human review workflow

Legal accuracy stakes make an unreviewed LLM answer risky to rely on
as-is. The Contract Register page (§7) shows every field with its
citation and a `CONFIDENCE` badge, and lets a reviewer mark `IS_VERIFIED`.
Fields extracted as `NOT_FOUND` or `LOW` confidence surface first —
this is the review queue, not a blocker to using the tool day one.

### 6.4 Securities reconciliation (Phase 3+)

"Reconciliation against BG/Cash list Maureen" implies a separate
maintained register (spreadsheet) outside the contract documents
themselves. Once named, this can be ingested the same way `ORG_MM_CHAT`
natively parses `.xlsx` registers (`xlsx_parser.py`, stdlib-only —
important, see the `org_mm_chat` README's `openpyxl`-breaks-warehouse-
runtime gotcha), loaded into a `SECURITIES_REGISTER` table, and joined
against `CONTRACT_FIELD_EXTRACTS.SECURITIES` in a
`SECURITIES_RECONCILIATION` view flagging mismatches. Scoped as a
fast-follow, not Build-phase MVP.

---

## 7. Streamlit application

| Page | Purpose |
|---|---|
| **Chat** (default/landing, per the template's convention) | Free-form Q&A per contract or across the whole corpus; every answer shows numbered, clickable citations |
| **Contract Register** *(new)* | One row per `CONTRACT_REGISTER` entry; expand a row to see all 16 stock fields with citation + confidence + verify checkbox; filter by status/confidence/reviewer |
| **Data Sources** (reused) | SharePoint folder browse/ingest, upload fallback |
| **Sync Status** (reused) | Ingestion run history, document/index counts |
| **Admin** *(new, optional)* | Contract-family linking review/override, re-run extraction, view `PROJECT_QUERY_LOG` |

No chat memory in v1 (each question stateless) — noted in the UI itself
("This assistant doesn't remember earlier questions in this session yet")
so users aren't surprised, and tracked as a v2 candidate (session-scoped
`st.session_state` history feeding into the synthesis prompt as prior
turns — not a schema change, since nothing needs to persist across
sessions for v2 either).

---

## 8. Response style

Every answer (stock-question or free-form) goes through one shared
synthesis instruction: concise, complete, professional tone, numbered
citations. This is a prompt-level policy on `query_engine.py`'s synthesis
call — no new mechanism needed, just an explicit style instruction added
to the existing prompt (today's is meeting-minutes-flavored; LEX's needs
"answer as if briefing a contracts manager," not "list agenda items").

---

## 9. Security & isolation

| Requirement | Design |
|---|---|
| Dedicated Database | `PROJECTS` gains a `DATA_DATABASE` column (default `'MEDSOCMS'` for existing/other projects — zero impact on `ORG_MM_CHAT`); LEX's row sets it to a new `LEXDB`. `config.py`'s `ProjectConfig.qualified_schema` reads `DATA_DATABASE` instead of the hardcoded `DATABASE` constant. Small, backward-compatible template change (§11 Phase 0). |
| Dedicated Schema | `LEXDB.DATA_LEX` — same `CREATE_PROJECT` mechanism, just pointed at the new database |
| Dedicated Compute Pool | `LEX_COMPUTE_POOL` (container runtime), sized independently of `STREAMLIT_COMPUTE_POOL_OCMS_BUSPERF` — 500-page OCR + 600-document indexing runs are heavier than meeting minutes |
| 3–5 named users only | New role `LEX_USERS`, granted `USAGE` on the Streamlit app, `LEXDB`, `DATA_LEX`, `LEX_COMPUTE_POOL`, and the query warehouse — granted **only** to the 3–5 named individuals (or a matching AD/SSO group with exactly those members), never to `PUBLIC` or a broad analyst role |
| Service account least privilege | Dedicated Graph app registration, `Sites.Selected` scoped to only the contracts library (§3); client secret in its own secret object, allow-listed only on LEX's own External Access Integration |
| Network path | MTM Azure network ↔ Snowflake over Private Link (existing account-level config — nothing LEX-specific to build) |
| Cross-region Cortex | Account has cross-region inference enabled to `AWS_AU` — LEX's `ACTIVE_MODEL`/`AI_PARSE_DOCUMENT`/`AI_EMBED` calls may execute there. No document *storage* leaves the Azure account/region; only the inference call does. Worth a one-line data-handling confirmation from Snowflake/compliance given contracts sensitivity (§13). |

See `ARCHITECTURE.md` for the visual (contextual/"solution on a page")
version of this table, including the security perimeter.

---

## 10. Non-functional / scale plan

| Dimension | Build (8 docs) | Scale (600 docs) |
|---|---|---|
| Indexing | Single-pass per document is fine even with the §5.3 fix (low volume) | Chunked indexing (§5.3) mandatory; batch via a Task, not inline Streamlit clicks |
| Retrieval | `ENABLE_VECTOR_SEARCH`/`ENABLE_RERANKING` can be validated either way | Both **on** — document-routing alone over 600 summaries is too coarse; needs the hybrid signal from day one of Scale |
| `MAX_CANDIDATE_DOCS` | Default (10) is fine | May need the per-project clamp revisited if genuinely cross-contract questions become common (rare for this use case — most questions are per-contract) |
| Extraction job | Runs inline in Streamlit after ingest | Snowflake Task on a schedule (e.g. nightly) + on-demand re-run for a single contract from the UI |
| Warehouse sizing | `MTMWH02` default is likely fine | Revisit warehouse size once OCR/indexing throughput on real 500-page PDFs is measured in Build phase |
| Monitoring | `PROJECT_SYNC_LOG`/`PROJECT_QUERY_LOG` (reused) | Same tables; consider a lightweight admin view once volume makes eyeballing logs impractical |

---

## 11. Execution plan

**Phase 0 — Bring the template into this repo, plus LEX extensions**
- Copy/adapt the `project-llm-wiki` engine from `org_mm_chat` into this repo (`sql/`, `python/`, `streamlit/`, `pipeline/`)
- Add `PROJECTS.DATA_DATABASE` column + `config.py` change (§9)
- Add `LEX_CONTRACT` segmentation profile (§5.1)
- Add `restrict_to_doc_ids` param to `query_engine.search()` (§6.2)
- Add `CONTRACT_REGISTER` / `CONTRACT_DOCUMENT_LINK` / `CONTRACT_FIELD_EXTRACTS` DDL to a new `sql/03_lex_schema.sql`
- Fix the §5.3 chunked-indexing gap
- Provision `LEXDB`, `LEX_COMPUTE_POOL`, dedicated Graph app registration, `LEX_USERS` role

**Phase 1 — Build (8 contracts)**
- Ingest the 8 signed contracts (+ any of their variations/extensions) via Data Sources
- Manually confirm contract-family links
- Run extraction job for all 16 fields; human-review every field (small volume — full review is feasible and sets the accuracy baseline)
- Validate free-form Q&A against known-answer questions from the team
- Tune the `LEX_CONTRACT` segmentation prompt and synthesis tone based on real review feedback

**Phase 2 — Validation / sign-off**
- Team reviews accuracy of all 16 fields across all 8 contracts + a sample of free-form questions
- Confirm citation correctness (right document, right clause) — this is the trust gate before scaling
- Go/no-go decision to proceed to Scale

**Phase 3 — Scale (up to 600 contracts)**
- Bulk SharePoint ingestion (batched, not all at once — watch OCR/Cortex throughput and cost)
- Switch on chunked indexing at volume, monitor `PROJECT_SYNC_LOG` for failures
- Move extraction job to a scheduled Task
- Spot-review a statistically meaningful sample (not every field of every contract) rather than full manual review

**Phase 4 — Future enhancements (not in scope now)**
- Conversational memory (§7)
- Securities reconciliation view (§6.4)
- Broader RBAC (self-service access beyond 3–5 users, if the team grows)

---

## 12. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Long contracts silently under-indexed (§5.3) | Fix before Build phase validation, not discovered at Scale |
| Wrong contract-family linking merges two unrelated contracts' answers | Auto-link is suggestion-only; always user-confirmable in UI |
| LLM hallucination on a legal field (e.g. termination notice period) | Every field carries a citation + verbatim quote; `IS_VERIFIED` gate; "not found" is an explicit valid answer, never a guess |
| Cost/throughput at 600×500-page OCR + indexing | Batch ingestion, chunked indexing, and Build-phase throughput measurement before committing a Scale-phase timeline |
| Cross-region inference + contract sensitivity | One-line confirmation from Snowflake/compliance (§13) before Build phase starts on real signed contracts |
| Source is actually a raw file share, not SharePoint (§3) | Confirm early — different ingestion connector needed if so |

---

## 13. Open questions for the team

1. Confirm the "Network Drive" is a SharePoint library (as assumed in §3) — if not, ingestion design changes.
2. Who are the 3–5 named users for `LEX_USERS`?
3. What identifies "the BG/Cash list Maureen [maintains]" (file name/location) for §6.4?
4. Any existing convention for CW-number formatting, to make auto-linking (§4.2) reliable?
5. Confirmation that Cortex cross-region inference to `AWS_AU` is acceptable for signed contract content, from a data-handling/compliance standpoint.
6. Desired cadence for the Scale-phase bulk ingestion (all at once vs. staged by contract value/complexity)?
