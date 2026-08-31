-- ============================================================================
-- LEX : 03_lex_contract_tables.sql
--
-- LEX-specific extension to the generic project-llm-wiki shape (RAW_DOCUMENTS
-- / DOCUMENT_INDEX, from 02_project_schema_template.sql). These three tables
-- are what let LEX answer the standard questions automatically and persist
-- the answers ("History"), and let a contract's variations/extensions/
-- novations be tracked as one logical contract rather than unrelated
-- documents.
--
-- Reference copy only — pipeline/00_provision_project.ipynb's "LEX contract
-- tables" cell runs the equivalent CREATE TABLE IF NOT EXISTS statements
-- directly against the project's resolved DATA_DATABASE.DATA_SCHEMA, the
-- same way the rest of this template avoids hand-edited {{...}} files at
-- run time. Safe to re-run any time (idempotent) — an already-provisioned
-- project that needs the newer columns picks them up from the notebook's
-- "Schema migrations" cell instead (ALTER TABLE ... ADD COLUMN IF NOT
-- EXISTS), the same forward-only pattern the rest of this template uses.
--
-- Replace {{DATA_DATABASE}} (e.g. MEDSCOMA) and {{DATA_SCHEMA}} (e.g.
-- DATA_LEX) before running by hand.
-- ============================================================================

USE ROLE ADVANCEDANALYTICS;
USE WAREHOUSE MTMWH02;
USE SCHEMA {{DATA_DATABASE}}.{{DATA_SCHEMA}};

-- One row per real-world contract — seeded from the team's Required
-- Contracts Register (an .xlsx listing every CW number in scope for the
-- current build/validation batch; see python/required_contracts.py), not
-- created ad hoc from whatever happens to get ingested. CW_NUMBER is the
-- business key a contract is actually known by — a contract can span many
-- RAW_DOCUMENTS rows over its life (base agreement, variations,
-- extensions, novation deeds).
CREATE TABLE IF NOT EXISTS CONTRACT_REGISTER (
    CONTRACT_ID       INT IDENTITY PRIMARY KEY,
    CW_NUMBER          VARCHAR(50) NOT NULL UNIQUE,
    CONTRACT_TITLE      VARCHAR(500),
    STATUS               VARCHAR(20) DEFAULT 'ACTIVE',  -- ACTIVE | EXPIRED | SUPERSEDED
    -- The template's "Executive Assessment" narrative (a few sentences),
    -- synthesized from the extracted stock fields once extraction has run
    -- — see contract_extraction.generate_contract_overview. Shown at the
    -- top of the Contract Lookup page and the downloadable .docx summary.
    OVERVIEW_SUMMARY      VARCHAR(4000),
    OVERVIEW_GENERATED_AT  TIMESTAMP_NTZ,
    -- The template's "Recommended Actions" bullet list (a JSON array of
    -- strings) and "Consolidated Procurement Assessment" scorecard (a JSON
    -- object keyed by contract_extraction.CLASSIFICATION_SCORECARD_FIELDS)
    -- — both synthesized alongside OVERVIEW_SUMMARY, from the same
    -- extracted stock fields, so all three stay consistent with each other.
    RECOMMENDED_ACTIONS        VARIANT,
    CLASSIFICATION_SCORECARD    VARIANT,
    CREATED_AT                    TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);

-- Which physical documents (RAW_DOCUMENTS rows) make up a contract's
-- history, and each one's role in that history. The stock-field extraction
-- job routes across every DOC_ID linked to a CONTRACT_ID, not just the base
-- document, so "what's the current end date" naturally considers the most
-- recent extension.
CREATE TABLE IF NOT EXISTS CONTRACT_DOCUMENT_LINK (
    LINK_ID          INT IDENTITY PRIMARY KEY,
    CONTRACT_ID       INT NOT NULL REFERENCES CONTRACT_REGISTER(CONTRACT_ID),
    DOC_ID             INT NOT NULL REFERENCES RAW_DOCUMENTS(DOC_ID),
    DOC_ROLE            VARCHAR(20) NOT NULL,  -- BASE | VARIATION | EXTENSION | NOVATION | DEED_OF_AMENDMENT
    EFFECTIVE_DATE       DATE,
    SEQUENCE_NO           INT,                  -- ordering within the family
    LINKED_BY              VARCHAR(200),          -- 'AUTO' (CW-number match) or a user
    LINKED_AT               TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
    UNIQUE (CONTRACT_ID, DOC_ID)
);

-- One row per (contract, stock field) — the persisted "History" the
-- Contract Lookup page reads from instead of recomputing on every visit.
-- Re-resolved only when a new/changed document lands in a contract's
-- family (see contract_extraction.is_extraction_current), never on a
-- plain page view. FIELD_VALUE/SOURCE_QUOTE/HIGHLIGHT_PHRASE/CONFIDENCE
-- are all produced by running the field's question through the same
-- query_engine.search() used for the (currently unused, but still
-- available) free-form search path — not a separate bespoke extraction
-- prompt.
CREATE TABLE IF NOT EXISTS CONTRACT_FIELD_EXTRACTS (
    EXTRACT_ID        INT IDENTITY PRIMARY KEY,
    CONTRACT_ID        INT NOT NULL REFERENCES CONTRACT_REGISTER(CONTRACT_ID),
    FIELD_KEY           VARCHAR(50) NOT NULL,
    FIELD_VALUE           VARCHAR(4000),          -- the concise answer
    SOURCE_DOC_ID           INT REFERENCES RAW_DOCUMENTS(DOC_ID),   -- which document in the family it was drawn from
    SOURCE_NODE_ID           INT REFERENCES DOCUMENT_INDEX(NODE_ID), -- supporting section, if any (may go stale after a reindex — SOURCE_QUOTE below is the durable copy)
    -- The cited section's excerpt text, copied verbatim at extraction time
    -- (not just a short snippet) — self-contained so the citation viewer
    -- still works even if the document is later reindexed and
    -- SOURCE_NODE_ID no longer resolves. This is what's shown as "cited
    -- passage" in the citation panel.
    SOURCE_QUOTE               VARCHAR(4000),
    -- A short exact phrase (a few words to one sentence), extracted from
    -- SOURCE_QUOTE and verified to be an exact substring of it — the
    -- specific text the citation viewer highlights/searches for, both in
    -- the plain-text panel and (best-effort) in the rendered original PDF.
    -- NULL when no reliable exact phrase could be extracted; the viewer
    -- falls back to showing SOURCE_QUOTE unhighlighted in that case.
    HIGHLIGHT_PHRASE            VARCHAR(500),
    CONFIDENCE                   VARCHAR(20),             -- HIGH | MEDIUM | LOW | NOT_FOUND
    MODEL_USED                    VARCHAR(50),
    EXTRACTED_AT                   TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
    IS_VERIFIED                     BOOLEAN DEFAULT FALSE,   -- human sign-off
    VERIFIED_BY                      VARCHAR(200),
    VERIFIED_AT                       TIMESTAMP_NTZ,
    UNIQUE (CONTRACT_ID, FIELD_KEY)
);
