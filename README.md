# LEX — Legal EXtraction & Contract Intelligence

LEX is a Snowflake Cortex AI + Streamlit tool for the MR5 Transition
Contracts Team: it answers a fixed set of stock questions for every
contract automatically (with citations), and lets users ask free-form
follow-up questions against the same contract corpus.

**Status:** Planning / proposal — no application code has landed in this
repo yet. This repo is the dedicated home for LEX going forward (split out
from an earlier planning pass done inside `ds-madhavan-ramani/org_mm_chat`).

## Start here

- **[PLAN.md](./PLAN.md)** — objective, data model, ingestion design,
  security/isolation approach, and the phased Build (8 contracts) → Scale
  (600 contracts) execution roadmap.
- **[ARCHITECTURE.md](./ARCHITECTURE.md)** — the one-page contextual
  architecture: users → Streamlit-in-Snowflake → Snowflake data model →
  SharePoint via Graph API, with the security perimeter and cross-region
  Cortex inference called out.
- A polished visual version of the same architecture diagram is published
  at: https://claude.ai/code/artifact/ef0664c5-268e-42b8-ab4e-553e9cbb785d

## Relationship to `project-llm-wiki`

LEX is designed as a **second project instance** of the `project-llm-wiki`
engine — the reusable multi-project Snowflake Cortex RAG template that
`ORG_MM_CHAT` runs on today, in
[`ds-madhavan-ramani/org_mm_chat`](https://github.com/ds-madhavan-ramani/org_mm_chat).
Every file path referenced in `PLAN.md` (e.g. `sql/00_setup_catalog.sql`,
`python/config.py`, `python/ingestion/index_builder.py`,
`streamlit/Chat.py`) refers to that template as it exists in the
`org_mm_chat` repo today — **not** to a path in this repo. Phase 0 of the
execution plan is to copy/adapt that engine into this repo (plus the LEX-
specific extensions `PLAN.md` §2 and §11 describe: a new `LEX_CONTRACT`
segmentation profile, the `CONTRACT_REGISTER` / `CONTRACT_FIELD_EXTRACTS`
tables, dedicated database/compute-pool isolation, and the chunked-
indexing fix for 100–500 page documents).
