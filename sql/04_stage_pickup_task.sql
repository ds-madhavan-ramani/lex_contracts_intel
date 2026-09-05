-- ============================================================================
-- LEX : 04_stage_pickup_task.sql
-- The "async loop": a scheduled Snowflake Task that drains
-- NETWORK_DRIVE_INBOX_STAGE (filled by the companion lex_network_bridge
-- repo's bridge tool) into this app's normal ingest pipeline —
-- python/ingestion/stage_pickup.py's run_stage_pickup().
--
-- Run from pipeline/00_provision_project.ipynb's "Set up the stage pickup
-- task" cell, AFTER the "Deploy the Streamlit app" cell — the stored
-- procedure below imports the same python/ tree that cell just staged to
-- MEDSCOMA.APP_CATALOG.LEX_APP_STAGE, so it needs to exist first.
--
-- UNVERIFIED: written without a live Snowflake account to test against.
-- Three things flagged below as the most likely places this needs
-- adjusting on first real run:
--   0. PACKAGES now includes 'python-docx' and 'reportlab' (added for
--      Phase 3 output caching — python/contract_output_cache.py, called
--      at the end of run_stage_pickup(), builds a Word/PDF summary via
--      docx_report.py/pdf_report.py from inside this procedure). Both are
--      pure-Python with no system dependencies, and reportlab in
--      particular is common enough in Snowflake's own stored-procedure
--      PDF-generation examples that it's very likely resolvable from this
--      account's Anaconda channel — but that's not independently
--      confirmed. If CREATE OR REPLACE PROCEDURE fails to resolve either
--      package, remove the contract_output_cache.cache_contract_outputs()
--      call from stage_pickup.py's _run_extraction_for_contracts (reverting
--      the Task to extraction-only) and rely on Streamlit's own calls to
--      contract_output_cache instead — python-docx already runs fine
--      there today (container runtime), and reportlab would just need
--      adding to streamlit/pyproject.toml and requirements.txt.
--   1. IMPORTS = ('@MEDSCOMA.APP_CATALOG.LEX_APP_STAGE/python/') assumes
--      Snowflake's Python stored-procedure IMPORTS mechanism, given a
--      stage *directory* path, extracts it the same way the Streamlit
--      app itself resolves these same modules (python/config.py ->
--      `import config`, python/ingestion/stage_pickup.py -> `from
--      ingestion import stage_pickup`) — consistent with every existing
--      absolute import in this codebase, but not independently confirmed
--      for the stored-procedure IMPORTS mechanism specifically. If it
--      doesn't resolve, list each required file/subdirectory as its own
--      IMPORTS entry instead of the single directory reference.
--   2. COPY FILES INTO <stage> FROM <stage> (used inside stage_pickup.py
--      for the stage-to-stage file copy) is assumed to work as documented;
--      see that module's own docstring for the GET/PUT fallback if not.
-- ============================================================================

USE ROLE ADVANCEDANALYTICS;
USE WAREHOUSE MTMWH02;
USE DATABASE MEDSCOMA;

-- ----------------------------------------------------------------------------
-- 1. Enable a directory table on the inbox stage — required for both
--    DIRECTORY(@stage) (what stage_pickup.list_staged_files() queries)
--    and CREATE STREAM ... ON STAGE below. Idempotent to re-run.
-- ----------------------------------------------------------------------------
ALTER STAGE MEDSCOMA.DATA_LEX.NETWORK_DRIVE_INBOX_STAGE
  SET DIRECTORY = (ENABLE = TRUE);

-- ----------------------------------------------------------------------------
-- 2. A stream on that stage — used purely as a scheduling gate (Task step
--    4 below only fires when SYSTEM$STREAM_HAS_DATA is true), not as the
--    data source for processing itself. stage_pickup.py re-lists the
--    stage's full directory table on every run and relies on its own
--    idempotent MERGE to skip files already picked up — this is a
--    deliberate self-healing choice (a run that fails partway, or a
--    stream that's ever recreated/reset, doesn't lose track of anything)
--    at the cost of one extra metadata scan per run, which is cheap.
-- ----------------------------------------------------------------------------
CREATE STREAM IF NOT EXISTS MEDSCOMA.DATA_LEX.NETWORK_DRIVE_INBOX_STREAM
  ON STAGE MEDSCOMA.DATA_LEX.NETWORK_DRIVE_INBOX_STAGE;

-- ----------------------------------------------------------------------------
-- 3. The stored procedure the Task calls. Imports the same python/ tree
--    the Streamlit app itself runs on (staged by the "Deploy the
--    Streamlit app" notebook cell), so stage_pickup.py's logic — and
--    everything it in turn calls (contract_linking, contract_extraction,
--    index_builder, and now contract_output_cache for the Word/PDF
--    summary cache) — is never duplicated here as inline SQL/Python.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE MEDSCOMA.APP_CATALOG.RUN_LEX_STAGE_PICKUP()
RETURNS VARCHAR
LANGUAGE PYTHON
RUNTIME_VERSION = '3.11'
PACKAGES = ('snowflake-snowpark-python', 'python-docx', 'reportlab')
IMPORTS = ('@MEDSCOMA.APP_CATALOG.LEX_APP_STAGE/python/')
HANDLER = 'run'
AS
$$
def run(session):
    # Consume the stream FIRST — SYSTEM$STREAM_HAS_DATA() in the task's
    # WHEN clause only checks the stream, it never advances its offset by
    # itself. Without this, the stream would stay "has data" forever
    # after the first file ever arrives, and the task would fire on every
    # schedule tick regardless of whether anything new actually landed. A
    # CREATE TABLE ... AS SELECT reading from the stream is one of
    # Snowflake's documented stream-consuming operations; its contents
    # are never used — only touching the stream at all is what matters
    # here, since the actual processing below re-lists the stage's full
    # directory table independently (see stage_pickup.py's own docstring
    # for why that redundancy is deliberate).
    session.sql(
        "CREATE OR REPLACE TEMPORARY TABLE MEDSCOMA.DATA_LEX._STAGE_PICKUP_STREAM_SNAPSHOT "
        "AS SELECT * FROM MEDSCOMA.DATA_LEX.NETWORK_DRIVE_INBOX_STREAM"
    ).collect()

    from ingestion.stage_pickup import run_stage_pickup
    return run_stage_pickup(session, 'LEX')
$$;

-- ----------------------------------------------------------------------------
-- 4. The task itself. Created SUSPENDED by default (Snowflake's own
--    behavior for CREATE TASK) — the RESUME statement at the bottom is
--    required before it will actually run on schedule.
--
--    SCHEDULE below is a starting point, not a fixed requirement — tune
--    to how quickly a staged file should show up as processed. The
--    stored procedure above consumes the stream on every run (resetting
--    SYSTEM$STREAM_HAS_DATA to false until the next new file arrives),
--    so a short interval doesn't mean constant warehouse spin-up when
--    nothing's changed.
--
--    CONFIRMED elsewhere in this account: EXECUTE TASK is typically an
--    ACCOUNTADMIN-only privilege to grant. If CREATE TASK or ALTER TASK
--    ... RESUME below fails for ADVANCEDANALYTICS, hand this to whoever
--    holds ACCOUNTADMIN:
--      GRANT EXECUTE TASK ON ACCOUNT TO ROLE ADVANCEDANALYTICS;
-- ----------------------------------------------------------------------------
CREATE TASK IF NOT EXISTS MEDSCOMA.APP_CATALOG.LEX_STAGE_PICKUP_TASK
  WAREHOUSE = MTMWH02
  SCHEDULE = '5 MINUTE'
  WHEN SYSTEM$STREAM_HAS_DATA('MEDSCOMA.DATA_LEX.NETWORK_DRIVE_INBOX_STREAM')
AS
  CALL MEDSCOMA.APP_CATALOG.RUN_LEX_STAGE_PICKUP();

ALTER TASK MEDSCOMA.APP_CATALOG.LEX_STAGE_PICKUP_TASK RESUME;

-- ----------------------------------------------------------------------------
-- Manual run (no need to wait for the schedule) / troubleshooting:
--   CALL MEDSCOMA.APP_CATALOG.RUN_LEX_STAGE_PICKUP();
--   SELECT * FROM TABLE(INFORMATION_SCHEMA.TASK_HISTORY(
--     TASK_NAME => 'LEX_STAGE_PICKUP_TASK')) ORDER BY SCHEDULED_TIME DESC;
--
-- Removal (e.g. before CALL TEARDOWN_PROJECT('LEX', ...) — that generic
-- proc doesn't know about this LEX-specific task, so suspend/drop it
-- first):
--   ALTER TASK MEDSCOMA.APP_CATALOG.LEX_STAGE_PICKUP_TASK SUSPEND;
--   DROP TASK IF EXISTS MEDSCOMA.APP_CATALOG.LEX_STAGE_PICKUP_TASK;
--   DROP STREAM IF EXISTS MEDSCOMA.DATA_LEX.NETWORK_DRIVE_INBOX_STREAM;
--   DROP PROCEDURE IF EXISTS MEDSCOMA.APP_CATALOG.RUN_LEX_STAGE_PICKUP();
-- ============================================================================
