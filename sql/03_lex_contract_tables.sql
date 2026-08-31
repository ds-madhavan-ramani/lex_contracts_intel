-- ============================================================================
-- LEX : 03_lex_contract_tables.sql
--
-- LEX-specific extension to the generic project-llm-wiki shape (RAW_DOCUMENTS
-- / DOCUMENT_INDEX, from 02_project_schema_template.sql). These three tables
-- are what let LEX answer the 15 stock questions automatically and persist
-- the answers, and let a contract's variations/extensions/novations be
-- tracked as one logical contract rather than unrelated documents.
--
-- Reference copy only — pipeline/00_provision_project.ipynb's "LEX contract
-- tables" cell runs the equivalent CREATE TABLE IF NOT EXISTS statements
-- directly against the project's resolved DATA_DATABASE.DATA_SCHEMA, the
-- same way the rest of this template avoids hand-edited {{...}} files at
-- run time. Safe to re-run any time (idempotent).
--
-- Replace {{DATA_DATABASE}} (e.g. LEXDB) and {{DATA_SCHEMA}} (e.g.
-- DATA_LEX) before running by hand.
-- ============================================================================

USE ROLE ADVANCEDANALYTICS;
USE WAREHOUSE MTMWH02;
USE SCHEMA {{DATA_DATABASE}}.{{DATA_SCHEMA}};

-- One row per real-world contract. CW_NUMBER is the business key a contract
-- is actually known by — a contract can span many RAW_DOCUMENTS rows over
-- its life (base agreement, variations, extensions, novation deeds).
CREATE TABLE IF NOT EXISTS CONTRACT_REGISTER (
    CONTRACT_ID       INT IDENTITY PRIMARY KEY,
    CW_NUMBER          VARCHAR(50) NOT NULL UNIQUE,
    CONTRACT_TITLE      VARCHAR(500),
    STATUS               VARCHAR(20) DEFAULT 'ACTIVE',  -- ACTIVE | EXPIRED | SUPERSEDED
    CREATED_AT            TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);

-- Which physical documents (RAW_DOCUMENTS rows) make up a contract's
-- history, and each one's role in that history. Free-form Q&A and the
-- stock-field extraction job both route across every DOC_ID linked to a
-- CONTRACT_ID, not just the base document, so "what's the current end
-- date" naturally considers the most recent extension.
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

-- One row per (contract, stock field) — the persisted, citable answer to
-- each of the 15 stock questions (see python/contract_extraction.py's
-- STOCK_FIELDS), re-resolved whenever a new document lands in a contract's
-- family. FIELD_VALUE/SOURCE_QUOTE/CONFIDENCE are all produced by running
-- the field's question through the same query_engine.search() used for
-- free-form chat, scoped to this contract's linked documents — not a
-- separate bespoke extraction prompt.
CREATE TABLE IF NOT EXISTS CONTRACT_FIELD_EXTRACTS (
    EXTRACT_ID        INT IDENTITY PRIMARY KEY,
    CONTRACT_ID        INT NOT NULL REFERENCES CONTRACT_REGISTER(CONTRACT_ID),
    FIELD_KEY           VARCHAR(50) NOT NULL,
    FIELD_VALUE           VARCHAR(4000),          -- the concise answer
    SOURCE_DOC_ID           INT REFERENCES RAW_DOCUMENTS(DOC_ID),   -- which document in the family it was drawn from
    SOURCE_NODE_ID           INT REFERENCES DOCUMENT_INDEX(NODE_ID), -- supporting section, if any
    SOURCE_QUOTE               VARCHAR(2000),          -- verbatim clause text, for audit
    CONFIDENCE                   VARCHAR(20),             -- HIGH | MEDIUM | LOW | NOT_FOUND
    MODEL_USED                    VARCHAR(50),
    EXTRACTED_AT                   TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
    IS_VERIFIED                     BOOLEAN DEFAULT FALSE,   -- human sign-off
    VERIFIED_BY                      VARCHAR(200),
    VERIFIED_AT                       TIMESTAMP_NTZ,
    UNIQUE (CONTRACT_ID, FIELD_KEY)
);
