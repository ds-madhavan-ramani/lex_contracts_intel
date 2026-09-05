-- ============================================================================
-- LEX : 04_stage_pickup_task.sql
-- The "async loop": a scheduled Snowflake Task that drains
-- NETWORK_DRIVE_INBOX_STAGE (filled by the companion lex_network_bridge
-- repo's bridge tool) into this app's normal ingest pipeline —
-- python/ingestion/stage_pickup.py's run_stage_pickup().
--
-- Run from pipeline/00_provision_project.ipynb's "Set up the stage pickup
-- task" cell, AFTER the "Deploy the Streamlit app" cell — that cell now
-- also zips python/'s contents and stages the zip this procedure's
-- IMPORTS references (see below), so it needs to run first.
--
-- CONFIRMED on a live account: PACKAGES resolves 'python-docx' and
-- 'reportlab' fine (CREATE OR REPLACE PROCEDURE succeeds, and CALL
-- RUN_LEX_STAGE_PICKUP() starts executing) — no PACKAGES-related fallback
-- needed. Also confirmed and fixed:
--   1. CREATE TEMPORARY TABLE is not usable inside a Python stored
--      procedure ("Unsupported statement type 'temporary TABLE'") — fixed
--      below by dropping TEMPORARY.
--   2. IMPORTS given a bare stage *directory* path
--      (`@LEX_APP_STAGE/python/`) does NOT flatten that directory's
--      contents onto sys.path the way Streamlit's own
--      sys.path.insert() does — confirmed on a live account as
--      `ModuleNotFoundError: No module named 'ingestion'` on the very
--      first import. Fixed by having the notebook cell zip python/'s
--      contents (files at the zip ROOT — config.py, ingestion/stage_pickup.py,
--      etc., no extra 'python/' folder inside) and referencing that zip
--      here instead — Snowflake's documented, reliable way to import a
--      multi-file/multi-package Python tree into a stored procedure.
--
-- Still UNVERIFIED: COPY FILES INTO <stage> FROM <stage> (used inside
-- stage_pickup.py for the stage-to-stage file copy) is assumed to work as
-- documented; see that module's own docstring for the GET/PUT fallback if
-- not.
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
--    the Streamlit app itself runs on, packaged as a zip (see the "Set up
--    the stage pickup task" notebook cell — a bare stage directory
--    reference doesn't work here, see this file's header comment), so
--    stage_pickup.py's logic — and everything it in turn calls
--    (contract_linking, contract_extraction, index_builder, and now
--    contract_output_cache for the Word/PDF summary cache) — is never
--    duplicated here as inline SQL/Python.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE MEDSCOMA.APP_CATALOG.RUN_LEX_STAGE_PICKUP()
RETURNS VARCHAR
LANGUAGE PYTHON
RUNTIME_VERSION = '3.11'
PACKAGES = ('snowflake-snowpark-python', 'python-docx', 'reportlab')
IMPORTS = ('@MEDSCOMA.APP_CATALOG.LEX_APP_STAGE/lex_python_imports.zip')
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
    #
    # CONFIRMED on a live account: TEMPORARY is not usable here — "Unsupported
    # statement type 'temporary TABLE'" from inside a Python stored procedure.
    # A permanent table works fine; CREATE OR REPLACE keeps it from
    # accumulating (each run just overwrites the same single-purpose table),
    # and it lives inside DATA_LEX so TEARDOWN_PROJECT('LEX', ...) removes it
    # along with everything else in that schema — no separate cleanup needed.
    session.sql(
        "CREATE OR REPLACE TABLE MEDSCOMA.DATA_LEX._STAGE_PICKUP_STREAM_SNAPSHOT "
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
