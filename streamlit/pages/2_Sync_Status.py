"""
pages/2_Sync_Status.py — ingestion/index counts, required-contracts
coverage, and recent run history.

Forked from the project-llm-wiki template.
Difference from that template: adds the required-contracts coverage
section — LEX's unit of progress is "how many of the required CW numbers
are fully extracted", not just raw document/index counts.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

import streamlit as st
from snowflake_session import get_session
from config import DATABASE, CATALOG_SCHEMA
import required_contracts

st.set_page_config(page_title="Sync Status — LEX", page_icon="📊", layout="wide")

if "project" not in st.session_state:
    st.warning("Select a project on the home page first.")
    st.stop()

session = get_session()
project = st.session_state["project"]
schema = project.qualified_schema

st.title(f"📊 Sync Status — {project.project_name}")

# LEX-specific: not part of the generic project-llm-wiki template. Runs
# the same stage-pickup logic the scheduled Task calls automatically every
# 5 minutes (see sql/04_stage_pickup_task.sql) — this button exists so
# there's no need to wait for the schedule, or open a SQL worksheet, to
# force a run right after staging files via the companion
# lex_network_bridge repo. The result is stashed in session_state and
# rendered AFTER the rerun below (a message shown just before st.rerun()
# is torn down before it's ever visible), then popped so it only shows once.
if st.session_state.get("stage_pickup_result"):
    st.success(st.session_state.pop("stage_pickup_result"))
if st.session_state.get("stage_pickup_error"):
    st.error(st.session_state.pop("stage_pickup_error"))

if st.button("🔄 Check for new files now", type="primary"):
    with st.spinner("Draining NETWORK_DRIVE_INBOX_STAGE…"):
        try:
            st.session_state["stage_pickup_result"] = session.sql(
                "CALL MEDSCOMA.APP_CATALOG.RUN_LEX_STAGE_PICKUP()"
            ).collect()[0][0]
        except Exception as e:  # noqa: BLE001 — surface it, don't crash the page
            st.session_state["stage_pickup_error"] = (
                f"Couldn't run the stage pickup: {e}\n\n"
                "Most likely cause: the \"Set up the stage pickup task\" "
                "notebook cell hasn't been run yet (the stored procedure "
                "doesn't exist)."
            )
    st.rerun()

st.divider()
st.subheader("Required contracts coverage")
required = required_contracts.list_required_contracts_status(session, project)
r_col1, r_col2, r_col3 = st.columns(3)
extracted = sum(1 for r in required if r.extracted_field_count > 0)
current = sum(1 for r in required if r.is_extraction_current)
r_col1.metric("Required contracts", len(required))
r_col2.metric("Extracted at least once", extracted)
r_col3.metric("Extraction current", current)
if required:
    st.dataframe(
        [{"CW Number": r.cw_number, "Title": r.contract_title or "",
          "Linked documents": r.linked_document_count,
          "Fields extracted": r.extracted_field_count,
          "Extraction current": "✅" if r.is_extraction_current else ("—" if not r.extracted_field_count else "⚠️")}
         for r in required],
        use_container_width=True, hide_index=True,
    )
else:
    st.info("No contracts in the Required Contracts Register yet — see Data Sources.")

st.divider()
st.subheader("Ingestion / indexing")
col1, col2, col3 = st.columns(3)
doc_count = session.sql(f"SELECT COUNT(*) AS C FROM {schema}.RAW_DOCUMENTS").collect()[0]["C"]
indexed_count = session.sql(
    f"SELECT COUNT(DISTINCT DOC_ID) AS C FROM {schema}.DOCUMENT_INDEX"
).collect()[0]["C"]
node_count = session.sql(f"SELECT COUNT(*) AS C FROM {schema}.DOCUMENT_INDEX").collect()[0]["C"]

col1.metric("Documents ingested", doc_count)
col2.metric("Documents indexed", indexed_count)
col3.metric("Index nodes", node_count)

if doc_count > indexed_count:
    st.warning(
        f"{doc_count - indexed_count} document(s) haven't been indexed yet — "
        "go to Data Sources → Index tab."
    )

st.subheader("Recent ingestion runs")
runs = session.sql(
    f"""SELECT RUN_TIMESTAMP, SOURCE_TYPE, FILES_FOUND, FILES_SYNCED, FILES_SKIPPED, FILES_FAILED
        FROM {DATABASE}.{CATALOG_SCHEMA}.PROJECT_SYNC_LOG
        WHERE PROJECT_ID = (SELECT PROJECT_ID FROM {DATABASE}.{CATALOG_SCHEMA}.PROJECTS
                            WHERE PROJECT_CODE = ?)
        ORDER BY RUN_TIMESTAMP DESC
        LIMIT 20""",
    params=[project.project_code],
).to_pandas()

if runs.empty:
    st.info("No ingestion runs yet.")
else:
    st.dataframe(runs, use_container_width=True)

st.subheader("Documents")
docs = session.sql(
    f"""SELECT FILE_NAME, SOURCE_TYPE, DOCUMENT_DATE, CREATED_AT
        FROM {schema}.RAW_DOCUMENTS
        ORDER BY CREATED_AT DESC"""
).to_pandas()
st.dataframe(docs, use_container_width=True)
