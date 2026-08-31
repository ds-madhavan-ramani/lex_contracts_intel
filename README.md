# LEX — Legal EXtraction & Contract Intelligence

LEX is a Snowflake Cortex AI + Streamlit tool for the MR5 Transition
Contracts Team: it answers a fixed set of stock questions for every
contract automatically (with citations), and lets users ask free-form
follow-up questions against the same contract corpus.

**Status:** Planning / proposal — no application code has landed in this
repo yet. This repo is the dedicated home for LEX going forward (split out
from an earlier planning pass done inside `ds-madhavan-ramani/org_mm_chat`).

## Start here

- **[LEX_Delivery_Plan.html](./LEX_Delivery_Plan.html)** ([published version](https://claude.ai/code/artifact/62475db9-d82d-41a0-b66e-1f85f2efbf4d))
  — the full delivery plan: scope, solution approach, architecture,
  security/access, answer-quality design, the phased Build (8 contracts)
  → Scale (600 contracts) execution roadmap, risks, and open decisions.
- **[LEX_Solution_Architecture.html](./LEX_Solution_Architecture.html)** ([published version](https://claude.ai/code/artifact/ef0664c5-268e-42b8-ab4e-553e9cbb785d))
  — the contextual block-diagram architecture: users, Streamlit-in-
  Snowflake, the `LEXDB` data model, the security perimeter (Private Link
  + cross-region Cortex inference to AWS AU), and a table of exactly what
  touches what by action.
- **[PLAN.md](./PLAN.md)** / **[ARCHITECTURE.md](./ARCHITECTURE.md)** —
  the same content in plain Markdown (Mermaid diagram included), for
  reading directly on GitHub without opening the HTML files.

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
