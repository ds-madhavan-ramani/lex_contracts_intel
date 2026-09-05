-- ============================================================================
-- LEX : 02_project_schema_template.sql
--
-- Reference copy of the DDL that PROJECTS.CREATE_PROJECT() applies
-- automatically. You normally never run this by hand — it exists so the
-- per-project table shape is reviewable/diffable outside the stored proc,
-- and as a fallback if you need to create a project's schema manually.
--
-- Replace {{DATA_DATABASE}} (e.g. MEDSCOMA) and {{DATA_SCHEMA}} (e.g.
-- DATA_LEX) before running. {{DATA_DATABASE}} must already exist.
-- ============================================================================

USE ROLE ADVANCEDANALYTICS;
USE WAREHOUSE MTMWH02;

CREATE SCHEMA IF NOT EXISTS {{DATA_DATABASE}}.{{DATA_SCHEMA}};
USE SCHEMA {{DATA_DATABASE}}.{{DATA_SCHEMA}};

CREATE STAGE IF NOT EXISTS DOCS_STAGE
    ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE');

CREATE TABLE IF NOT EXISTS RAW_DOCUMENTS (
    DOC_ID              INT IDENTITY PRIMARY KEY,
    FILE_NAME             VARCHAR(500) NOT NULL,
    STAGE_PATH             VARCHAR(1000) NOT NULL,
    SOURCE_TYPE             VARCHAR(20) NOT NULL,      -- 'UPLOAD' | 'NETWORK_DRIVE_STAGE'
    SOURCE_ITEM_ID           VARCHAR(1000),              -- dedup key: the bridge's inbox-stage relative
                                                          -- path ("<CW>/<filename>") for NETWORK_DRIVE_STAGE;
                                                          -- NULL for uploads
    DOCUMENT_DATE               DATE,
    RAW_TEXT                     VARCHAR(16777216),
    SOURCE_HASH                   VARCHAR(64),
    SOURCE_URL                     VARCHAR(2000),           -- external web link, if any — NULL for
                                                             -- network-drive/upload sources
    PARSED_AT                      TIMESTAMP_NTZ,
    CREATED_AT                      TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);

CREATE TABLE IF NOT EXISTS DOCUMENT_INDEX (
    NODE_ID          INT IDENTITY PRIMARY KEY,
    DOC_ID            INT NOT NULL REFERENCES RAW_DOCUMENTS(DOC_ID),
    PARENT_NODE_ID     INT,
    NODE_LEVEL           VARCHAR(20) NOT NULL,          -- 'document' | 'section'
    NODE_TITLE             VARCHAR(500),
    NODE_SUMMARY             VARCHAR(8000),   -- generous headroom for a thorough per-section paragraph
    NODE_TEXT_REF              VARCHAR(50),
    NODE_EMBEDDING               VECTOR(FLOAT, 768),  -- section-level only; see index_builder.py/query_engine.py
    CREATED_AT                  TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);

-- Notes on SEGMENTATION_PROFILE (set per-project on PROJECTS row):
--   'GENERIC'      — document -> section tree, no assumptions about content type.
--   'LEX_CONTRACT' — clause/schedule-aware sectioning for legal contracts;
--                    see python/ingestion/index_builder.py::PROMPTS['LEX_CONTRACT'].
--   Add new profiles by adding a key to PROMPTS in index_builder.py and
--   setting PROJECTS.SEGMENTATION_PROFILE to match — no schema change needed.
--
-- Contract-family tables (CONTRACT_REGISTER, CONTRACT_DOCUMENT_LINK,
-- CONTRACT_FIELD_EXTRACTS) are LEX-specific extensions, not part of the
-- generic project shape above — see sql/03_lex_contract_tables.sql.
