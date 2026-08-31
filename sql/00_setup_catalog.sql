-- ============================================================================
-- LEX : 00_setup_catalog.sql
-- One-time setup. Creates the central catalog schema shared across every
-- llm-wiki-style project on this account — including projects from OTHER
-- repos (e.g. ORG_MM_CHAT in ds-madhavan-ramani/org_mm_chat). Safe to
-- re-run: every statement is CREATE ... IF NOT EXISTS.
--
-- Infra values carried over unchanged from project-llm-wiki:
--   Warehouse : MTMWH02
--   Catalog   : MEDSOCMS.APP_CATALOG (shared, database is always MEDSOCMS)
--   Role      : ADVANCEDANALYTICS
--
-- LEX-specific difference from the org_mm_chat template this was forked
-- from: a project's actual DATA_SCHEMA can now live in its own DATABASE
-- (DATA_DATABASE), not just its own schema inside the shared MEDSOCMS
-- database. LEX uses this to get MEDSCOMA as a fully isolated database. The
-- catalog bookkeeping itself (this schema) stays centralized in MEDSOCMS
-- either way — only a project's actual documents/index move.
-- ============================================================================

USE ROLE ADVANCEDANALYTICS;
USE WAREHOUSE MTMWH02;
USE DATABASE MEDSOCMS;

CREATE SCHEMA IF NOT EXISTS MEDSOCMS.APP_CATALOG;
USE SCHEMA MEDSOCMS.APP_CATALOG;

-- ----------------------------------------------------------------------------
-- PROJECTS : one row per project instance of the template. A project's data
-- lives at DATA_DATABASE.DATA_SCHEMA (DATA_DATABASE defaults to 'MEDSOCMS',
-- matching every project created before this column existed) and owns that
-- schema's RAW_DOCUMENTS / DOCUMENT_INDEX tables. Everything a Streamlit
-- session or ingestion job needs to behave "per project" lives on this row,
-- not in a config.py file.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS PROJECTS (
    PROJECT_ID              INT IDENTITY PRIMARY KEY,
    PROJECT_CODE            VARCHAR(50) NOT NULL UNIQUE,   -- e.g. 'LEX', short, upper snake_case
    PROJECT_NAME             VARCHAR(200) NOT NULL,         -- display name in Streamlit
    DESCRIPTION              VARCHAR(1000),
    DATA_DATABASE             VARCHAR(100) NOT NULL DEFAULT 'MEDSOCMS',  -- e.g. 'MEDSCOMA' — must already
                                                                          -- exist (created separately,
                                                                          -- typically by SYSADMIN); this
                                                                          -- template does not create databases
    DATA_SCHEMA               VARCHAR(100) NOT NULL,          -- e.g. 'DATA_LEX'
    STAGE_NAME                VARCHAR(100) NOT NULL,          -- e.g. 'DOCS_STAGE' (unqualified, lives in DATA_SCHEMA)

    -- Deployment identity + compute — each project gets its own Streamlit
    -- app object and can run on its own warehouse/compute pool. Nothing
    -- about infra is shared implicitly; every project row is self-describing.
    STREAMLIT_APP_NAME          VARCHAR(100) NOT NULL,          -- e.g. 'LEX_APP' (unqualified, lives in APP_CATALOG)
    STREAMLIT_STAGE_NAME         VARCHAR(100) NOT NULL,          -- e.g. 'LEX_APP_STAGE' (unqualified, lives in APP_CATALOG)
    QUERY_WAREHOUSE                VARCHAR(100) NOT NULL DEFAULT 'MTMWH02',
    COMPUTE_POOL                    VARCHAR(100),                   -- NULL = warehouse runtime; set = container runtime

    -- SharePoint source config (optional — a project can be file-upload-only)
    SHAREPOINT_SITE_URL       VARCHAR(500),
    SHAREPOINT_DEFAULT_FOLDER VARCHAR(1000),

    -- Optional per-project Graph API app registration, for least-privilege
    -- ingestion (a dedicated service account scoped to only this project's
    -- library, rather than the shared tenant-level app every project uses
    -- by default). All three NULL (the default) means "use the shared
    -- tenant-level GRAPH_TENANT_ID/GRAPH_CLIENT_ID/GRAPH_SECRET_NAME
    -- constants in python/config.py" — see config.py's fallback logic.
    -- GRAPH_TENANT_ID/GRAPH_CLIENT_ID are not secret (Microsoft surfaces
    -- both on the app registration's own Overview page), so they're plain
    -- columns here like everything else on this row; only the secret's
    -- *name* is stored — its value lives in a Snowflake SECRET object,
    -- created and bound separately (see sql/test_graph_connectivity.sql).
    GRAPH_TENANT_ID            VARCHAR(100),
    GRAPH_CLIENT_ID            VARCHAR(100),
    GRAPH_SECRET_NAME          VARCHAR(200),   -- fully qualified, e.g. 'MEDSOCMS.APP_CATALOG.LEX_GRAPH_API_SECRET'

    -- Per-project model / tuning knobs (were hardcoded in config.py)
    ACTIVE_MODEL               VARCHAR(50)  DEFAULT 'claude-haiku-4-5',
    MAX_DOCUMENT_CHARS          INT          DEFAULT 150000,   -- also doubles as the per-chunk size for
                                                                -- documents longer than this — see
                                                                -- ingestion/index_builder.py's chunked
                                                                -- indexing (needed for 100-500 page
                                                                -- contracts; a single call this size
                                                                -- would truncate, not chunk, before
                                                                -- that fix existed)
    MAX_SECTION_CHARS           INT          DEFAULT 8000,
    QUERY_CACHE_TTL_HOURS        INT          DEFAULT 24,
    MAX_CITATIONS_DISPLAY         INT          DEFAULT 5,

    -- Segmentation behaviour: which prompt template to use when building the
    -- tree index. 'GENERIC' works for most document types. Projects can
    -- register a specialized one (see 02_project_schema_template.sql notes).
    SEGMENTATION_PROFILE           VARCHAR(50)  DEFAULT 'GENERIC',
    -- 'STANDARD' (one section per natural break) or 'DETAILED' (push the
    -- indexing model to further split each break into per-topic/per-clause
    -- sections) — see index_builder.py's SEGMENTATION_GRANULARITY_INSTRUCTIONS.
    -- Orthogonal to SEGMENTATION_PROFILE: any profile can run at either
    -- granularity.
    SEGMENTATION_GRANULARITY        VARCHAR(20)  DEFAULT 'STANDARD',

    -- Retrieval mechanism toggles — not every LLM Wiki deployment needs
    -- every mechanism. MAX_CANDIDATE_DOCS is clamped to [5, 10] in
    -- query_engine.py, not enforced by a DB CHECK constraint — Snowflake
    -- accepts CHECK syntax but never actually enforces it.
    ENABLE_RERANKING                 BOOLEAN      DEFAULT TRUE,   -- LLM judges/filters the section
                                                                   -- candidate pool before synthesis,
                                                                   -- vs. using it directly (cheaper/faster)
    ENABLE_VECTOR_SEARCH              BOOLEAN      DEFAULT FALSE,  -- AI_EMBED-based semantic search as a
                                                                   -- third retrieval signal (needs a
                                                                   -- reindex to populate NODE_EMBEDDING)
    MAX_CANDIDATE_DOCS                 INT          DEFAULT 10,     -- clamped to [5, 10] in query_engine.py

    STATUS                          VARCHAR(20)  DEFAULT 'ACTIVE',  -- ACTIVE | ARCHIVED
    CREATED_BY                       VARCHAR(200),
    CREATED_AT                        TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);

-- ----------------------------------------------------------------------------
-- Shared logs across all projects (PROJECT_ID discriminates). Kept centralized
-- rather than per-schema so there's one place to build a cross-project admin
-- view later; revisit if per-project isolation becomes a requirement.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS PROJECT_SYNC_LOG (
    RUN_ID          INT IDENTITY PRIMARY KEY,
    PROJECT_ID      INT NOT NULL REFERENCES PROJECTS(PROJECT_ID),
    SOURCE_TYPE     VARCHAR(20) NOT NULL,       -- 'UPLOAD' | 'SHAREPOINT'
    RUN_TIMESTAMP   TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
    FILES_FOUND     INT DEFAULT 0,
    FILES_SYNCED    INT DEFAULT 0,
    FILES_SKIPPED   INT DEFAULT 0,
    FILES_FAILED    INT DEFAULT 0,
    TRIGGERED_BY    VARCHAR(200),
    DETAIL          VARIANT
);

CREATE TABLE IF NOT EXISTS PROJECT_QUERY_LOG (
    QUERY_ID        INT IDENTITY PRIMARY KEY,
    PROJECT_ID      INT NOT NULL REFERENCES PROJECTS(PROJECT_ID),
    USER_QUESTION   VARCHAR(2000),
    QUERY_HASH      VARCHAR(64),
    NODES_VISITED   VARIANT,
    FINAL_ANSWER    VARCHAR(16000),
    CITED_DOCS      VARIANT,
    LATENCY_MS      INT,
    CREATED_AT      TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);

-- ----------------------------------------------------------------------------
-- CREATE_PROJECT : provisions a brand-new project end-to-end.
--   1. Validates project_code
--   2. Creates <data_database>.DATA_<CODE> schema (data_database must
--      already exist — see the DATA_DATABASE column note above)
--   3. Creates that schema's RAW_DOCUMENTS / DOCUMENT_INDEX tables + stage
--   4. Inserts the PROJECTS catalog row
-- Callable from SQL directly, or from python/provisioning/create_project.py.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE CREATE_PROJECT(
    PROJECT_CODE             VARCHAR,
    PROJECT_NAME              VARCHAR,
    DESCRIPTION               VARCHAR,
    SHAREPOINT_SITE_URL        VARCHAR,
    SHAREPOINT_DEFAULT_FOLDER   VARCHAR,
    CREATED_BY                   VARCHAR,
    QUERY_WAREHOUSE                VARCHAR,   -- pass '' or NULL to use the default 'MTMWH02'
    COMPUTE_POOL                    VARCHAR,   -- pass '' or NULL for warehouse runtime (no compute pool)
    DATA_DATABASE                    VARCHAR    -- pass '' or NULL to use the default 'MEDSOCMS'
)
RETURNS VARCHAR
LANGUAGE PYTHON
RUNTIME_VERSION = '3.11'
PACKAGES = ('snowflake-snowpark-python')
HANDLER = 'run'
AS
$$
import re

def run(session, project_code, project_name, description,
        sharepoint_site_url, sharepoint_default_folder, created_by,
        query_warehouse, compute_pool, data_database):

    code = (project_code or "").strip().upper()
    if not re.match(r'^[A-Z][A-Z0-9_]{2,49}$', code):
        raise ValueError(
            "project_code must be 3-50 chars, start with a letter, "
            "and contain only A-Z, 0-9, _ (e.g. 'LEX')"
        )

    existing = session.sql(
        "SELECT COUNT(*) AS C FROM PROJECTS WHERE PROJECT_CODE = ?", params=[code]
    ).collect()
    if existing[0]["C"] > 0:
        raise ValueError(f"Project code '{code}' already exists")

    data_database = (data_database or "").strip().upper() or "MEDSOCMS"
    data_schema = f"DATA_{code}"
    stage_name = "DOCS_STAGE"
    streamlit_app_name = f"{code}_APP"
    streamlit_stage_name = f"{code}_APP_STAGE"
    query_warehouse = (query_warehouse or "").strip() or "MTMWH02"
    compute_pool = (compute_pool or "").strip()
    if compute_pool.lower() in ("", "none", "null"):
        compute_pool = None

    # 1. Create the project's isolated data schema. data_database must
    #    already exist — this proc does not create databases (typically a
    #    SYSADMIN-only privilege the role running this proc doesn't hold).
    #    A non-default data_database that doesn't exist yet fails here with
    #    Snowflake's own "does not exist or not authorized" error.
    session.sql(f"CREATE SCHEMA IF NOT EXISTS {data_database}.{data_schema}").collect()

    # 2. Create its tables + stage (mirrors 02_project_schema_template.sql).
    #    NODE_SUMMARY is VARCHAR(8000) and NODE_EMBEDDING/SOURCE_URL are
    #    present from the start — these were later migrations on the
    #    original org_mm_chat template's already-existing project, but this
    #    is a fresh project so there's no earlier shape to migrate from.
    ddl_statements = [
        f"""CREATE STAGE IF NOT EXISTS {data_database}.{data_schema}.{stage_name}
              ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE')""",
        f"""CREATE TABLE IF NOT EXISTS {data_database}.{data_schema}.RAW_DOCUMENTS (
              DOC_ID              INT IDENTITY PRIMARY KEY,
              FILE_NAME            VARCHAR(500) NOT NULL,
              STAGE_PATH            VARCHAR(1000) NOT NULL,
              SOURCE_TYPE            VARCHAR(20) NOT NULL,   -- 'UPLOAD' | 'SHAREPOINT'
              SHAREPOINT_ITEM_ID      VARCHAR(200),           -- dedup key, NULL for uploads
              DOCUMENT_DATE             DATE,                    -- best-effort extracted date
              RAW_TEXT                   VARCHAR(16777216),
              SOURCE_HASH                 VARCHAR(64),             -- SHA256 of raw_text, idempotency
              SOURCE_URL                   VARCHAR(2000),            -- SharePoint webUrl, NULL for uploads
              PARSED_AT                    TIMESTAMP_NTZ,
              CREATED_AT                    TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
            )""",
        f"""CREATE TABLE IF NOT EXISTS {data_database}.{data_schema}.DOCUMENT_INDEX (
              NODE_ID          INT IDENTITY PRIMARY KEY,
              DOC_ID            INT NOT NULL REFERENCES {data_database}.{data_schema}.RAW_DOCUMENTS(DOC_ID),
              PARENT_NODE_ID     INT,                      -- NULL for the document-level root node
              NODE_LEVEL          VARCHAR(20) NOT NULL,      -- 'document' | 'section'
              NODE_TITLE            VARCHAR(500),
              NODE_SUMMARY           VARCHAR(8000),           -- generous headroom: a thorough per-section
                                                               -- paragraph can run well past a short gloss
              NODE_TEXT_REF            VARCHAR(50),             -- "start:end" offsets into RAW_TEXT
              NODE_EMBEDDING            VECTOR(FLOAT, 768),      -- section-level only (NULL for
                                                                  -- 'document' nodes); AI_EMBED('snowflake-arctic-embed-m', ...)
                                                                  -- at index time, for vector/semantic retrieval — see query_engine.py
              CREATED_AT                TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
            )""",
    ]
    for stmt in ddl_statements:
        session.sql(stmt).collect()

    # 3. Create this project's own Streamlit deployment stage (separate from
    #    its data stage — this one holds app code, not documents). Always
    #    in the shared MEDSOCMS.APP_CATALOG, regardless of data_database —
    #    app code isn't sensitive contract data.
    session.sql(
        f"CREATE STAGE IF NOT EXISTS MEDSOCMS.APP_CATALOG.{streamlit_stage_name}"
    ).collect()

    # 4. Register in the catalog
    session.sql(
        """INSERT INTO PROJECTS
           (PROJECT_CODE, PROJECT_NAME, DESCRIPTION, DATA_DATABASE, DATA_SCHEMA, STAGE_NAME,
            STREAMLIT_APP_NAME, STREAMLIT_STAGE_NAME, QUERY_WAREHOUSE, COMPUTE_POOL,
            SHAREPOINT_SITE_URL, SHAREPOINT_DEFAULT_FOLDER, CREATED_BY)
           SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?""",
        params=[code, project_name, description, data_database, data_schema, stage_name,
                streamlit_app_name, streamlit_stage_name, query_warehouse, compute_pool,
                sharepoint_site_url, sharepoint_default_folder, created_by],
    ).collect()

    return (f"Project '{code}' created. Data schema {data_database}.{data_schema} and "
            f"deploy stage MEDSOCMS.APP_CATALOG.{streamlit_stage_name} are ready. "
            f"Run the deploy notebook cell next to create MEDSOCMS.APP_CATALOG.{streamlit_app_name}.")
$$;

-- ----------------------------------------------------------------------------
-- TEARDOWN_PROJECT : drops a project's schema (from wherever DATA_DATABASE
-- says it lives) and removes its catalog row. Logs (PROJECT_SYNC_LOG /
-- PROJECT_QUERY_LOG) are left in place for audit history unless purge_logs
-- = TRUE. Never drops DATA_DATABASE itself (e.g. MEDSCOMA) — only the
-- project's own schema inside it, since other projects/objects may share
-- that database.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE TEARDOWN_PROJECT(PROJECT_CODE VARCHAR, PURGE_LOGS BOOLEAN)
RETURNS VARCHAR
LANGUAGE PYTHON
RUNTIME_VERSION = '3.11'
PACKAGES = ('snowflake-snowpark-python')
HANDLER = 'run'
AS
$$
def run(session, project_code, purge_logs):
    code = (project_code or "").strip().upper()
    rows = session.sql(
        """SELECT PROJECT_ID, DATA_DATABASE, DATA_SCHEMA, STREAMLIT_APP_NAME, STREAMLIT_STAGE_NAME
           FROM PROJECTS WHERE PROJECT_CODE = ?""", params=[code]
    ).collect()
    if not rows:
        raise ValueError(f"No project found with code '{code}'")

    r = rows[0]
    project_id, data_database, data_schema = r["PROJECT_ID"], r["DATA_DATABASE"], r["DATA_SCHEMA"]
    streamlit_app_name, streamlit_stage_name = r["STREAMLIT_APP_NAME"], r["STREAMLIT_STAGE_NAME"]

    session.sql(f"DROP STREAMLIT IF EXISTS MEDSOCMS.APP_CATALOG.{streamlit_app_name}").collect()
    session.sql(f"DROP STAGE IF EXISTS MEDSOCMS.APP_CATALOG.{streamlit_stage_name}").collect()
    session.sql(f"DROP SCHEMA IF EXISTS {data_database}.{data_schema} CASCADE").collect()
    session.sql("DELETE FROM PROJECTS WHERE PROJECT_ID = ?", params=[project_id]).collect()

    if purge_logs:
        session.sql("DELETE FROM PROJECT_SYNC_LOG WHERE PROJECT_ID = ?", params=[project_id]).collect()
        session.sql("DELETE FROM PROJECT_QUERY_LOG WHERE PROJECT_ID = ?", params=[project_id]).collect()

    return (f"Project '{code}' torn down: Streamlit app, its deploy stage, and "
            f"schema {data_database}.{data_schema} were dropped ({data_database} itself was not).")
$$;
