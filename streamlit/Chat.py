"""
streamlit/Chat.py — entry point AND the Contract Lookup UI itself.

Filename kept as Chat.py purely so pipeline/00_provision_project.ipynb's
`MAIN_FILE = 'Chat.py'` deploy setting doesn't need to change — there is no
chat feature here. Enter/select a contract number and get its standard
questions answered from History (CONTRACT_FIELD_EXTRACTS), laid out the
same way as the team's own Contract Workspace Summary Template, with a
citation panel on the side and matching .docx/.pdf exports (served from
contract_output_cache's stage cache when the async pickup Task has
already produced them; built live otherwise) — the free-form
question-answering engine this was originally built around
(query_engine.search()) is not a user-facing feature right now; it's still
what contract_extraction.py runs under the hood.

This deployment is dedicated to one project (LEX) — a separate "select a
project" landing screen before you can even see this page isn't useful for
a single-purpose app, so this page loads the project directly and *is* the
default view. If the catalog ever holds more than one active project, a
picker still appears — it just isn't the app's front door.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "python"))

import streamlit as st
from snowflake_session import get_session
from config import load_project, list_active_projects
import contract_linking
import contract_extraction
import contract_output_cache
import required_contracts
from citation_panel_ui import render_citation_panel

st.set_page_config(page_title="Contract Lookup — LEX", page_icon="⚖️", layout="wide")

session = get_session()

st.sidebar.title("⚖️ LEX")
st.sidebar.caption("Legal EXtraction & Contract Intelligence")

projects = list_active_projects(session)
if not projects:
    st.sidebar.warning("No projects exist yet.")
    st.title("Welcome to LEX")
    st.write(
        "No projects have been created yet. This deployment is provisioned "
        "for the LEX project via `pipeline/00_provision_project.ipynb` — "
        "run that notebook's project-creation step, then reload this app."
    )
    st.stop()

if len(projects) == 1:
    selected_code = projects[0]["PROJECT_CODE"]
else:
    project_labels = {f"{p['PROJECT_NAME']} ({p['PROJECT_CODE']})": p["PROJECT_CODE"] for p in projects}
    selected_label = st.sidebar.selectbox("Active project", list(project_labels.keys()))
    selected_code = project_labels[selected_label]

st.session_state["project_code"] = selected_code
st.session_state["project"] = load_project(session, selected_code)
project = st.session_state["project"]

st.sidebar.divider()

st.title(f"🔍 Contract Lookup — {project.project_name}")
st.caption(
    "Enter a contract number to see its standard questions answered, laid "
    "out the same way as the Contract Workspace Summary Template, with "
    "citations back to the original document. Answers already extracted "
    "are served instantly from history — nothing is re-parsed or "
    "re-extracted unless the source document has actually changed."
)

required = required_contracts.list_required_contracts_status(session, project)
if not required:
    st.info(
        "No contracts in the Required Contracts Register yet. Go to **Data "
        "Sources** to upload the register workbook, then come back here."
    )
    st.stop()


def _option_label(r) -> str:
    if r.linked_document_count == 0:
        state = "not yet ingested"
    elif r.extracted_field_count == 0:
        state = f"{r.linked_document_count} doc(s), not yet extracted"
    elif not r.is_extraction_current:
        state = f"{r.linked_document_count} doc(s), documents changed since extraction"
    else:
        state = f"{r.linked_document_count} doc(s), extracted"
    title_bit = f" — {r.contract_title}" if r.contract_title else ""
    return f"{r.cw_number}{title_bit}  ·  {state}"


options = {_option_label(r): r for r in required}
selected_label = st.selectbox("Contract number", list(options.keys()))
status = options[selected_label]

st.divider()

if status.linked_document_count == 0:
    st.warning(
        f"**{status.cw_number}** has no documents linked yet. Ingest its signed/executed "
        "contract on the **Data Sources** page, then link it to this contract number on "
        "the **Contract Register** page."
    )
    st.stop()

if status.extracted_field_count == 0:
    st.info(f"**{status.cw_number}** has linked documents but hasn't been extracted yet.")
    if st.button("Run extraction now", type="primary"):
        with st.spinner("Answering the standard questions…"):
            contract_extraction.extract_stock_fields_for_contract(session, project, status.contract_id)
            contract_output_cache.cache_contract_outputs(session, project, status.contract_id)
        st.rerun()
    st.stop()

if not status.is_extraction_current:
    st.warning(
        f"A linked document for **{status.cw_number}** has changed since this was last "
        "extracted — the answers below may be out of date."
    )
    if st.button("Re-run extraction", type="primary"):
        with st.spinner("Re-answering the standard questions…"):
            contract_extraction.extract_stock_fields_for_contract(session, project, status.contract_id)
            contract_output_cache.cache_contract_outputs(session, project, status.contract_id)
        st.rerun()

contract = contract_linking.get_contract(session, project, status.contract_id)
fields_list = contract_extraction.get_contract_fields(session, project, status.contract_id)
fields = {f["FIELD_KEY"]: f for f in fields_list}
variations = contract_linking.get_significant_variations(session, project, status.contract_id)

header_col, docx_col, pdf_col = st.columns([4, 1, 1])
header_col.subheader(contract["CONTRACT_TITLE"] or contract["CW_NUMBER"])
header_col.caption(contract["CW_NUMBER"])

# Served from the cache the stage-pickup Task (or the extraction buttons
# above) already populated; falls back to building it live if nothing's
# cached yet — see contract_output_cache.get_or_build_output.
docx_col.download_button(
    "⬇ Word (.docx)",
    data=contract_output_cache.get_or_build_output(session, project, status.contract_id, "docx"),
    file_name=f"{contract['CW_NUMBER']}_Contract_Workspace_Summary.docx",
    mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
)
pdf_col.download_button(
    "⬇ PDF",
    data=contract_output_cache.get_or_build_output(session, project, status.contract_id, "pdf"),
    file_name=f"{contract['CW_NUMBER']}_Contract_Workspace_Summary.pdf",
    mime="application/pdf",
)

CONFIDENCE_BADGE = {
    "HIGH": "🟢 High",
    "MEDIUM": "🟡 Medium",
    "LOW": "🟠 Low — worth a look",
    "NOT_FOUND": "🔴 Not found in the documents",
    None: "⚪ Not yet extracted",
}

if "active_field" not in st.session_state:
    st.session_state["active_field"] = None

list_col, panel_col = st.columns([3, 2])

with list_col:
    if contract.get("OVERVIEW_SUMMARY"):
        st.markdown("### Executive Assessment")
        st.write(contract["OVERVIEW_SUMMARY"])
    else:
        st.caption("Executive Assessment not yet generated.")

    st.markdown("### Contract detail")
    for key in contract_extraction.CONTRACT_DETAIL_FIELDS:
        cols = st.columns([2, 3])
        cols[0].markdown(f"**{contract_extraction.FIELD_LABELS[key]}**")
        cols[1].write(fields[key]["FIELD_VALUE"] or "_Not yet extracted._")

    def _render_assessment_group(title: str, field_keys: list):
        st.markdown(f"### {title}")
        for key in field_keys:
            f = fields[key]
            with st.container(border=True):
                st.markdown(f"**{contract_extraction.FIELD_LABELS[key]}**")
                st.write(f["FIELD_VALUE"] or "_Not yet extracted._")
                badge_col, btn_col = st.columns([3, 1])
                badge_col.caption(
                    CONFIDENCE_BADGE.get(f.get("CONFIDENCE"), f.get("CONFIDENCE") or "⚪ Not yet extracted")
                    + (" · Verified" if f.get("IS_VERIFIED") else "")
                )
                if f.get("FIELD_VALUE") and f.get("SOURCE_DOC_ID"):
                    if btn_col.button("View source", key=f"src_{key}"):
                        st.session_state["active_field"] = key

    _render_assessment_group("Executive Assessment — findings", contract_extraction.EXECUTIVE_ASSESSMENT_FIELDS)

    st.markdown("### Significant Variations")
    if variations:
        for v in variations:
            role = v["DOC_ROLE"].replace("_", " ").title()
            date_bit = f", {v['EFFECTIVE_DATE']}" if v.get("EFFECTIVE_DATE") else ""
            label = f"[{v['FILE_NAME']}]({v['SOURCE_URL']})" if v.get("SOURCE_URL") else v["FILE_NAME"]
            st.markdown(f"- **{label}** ({role}{date_bit}): {v.get('NODE_SUMMARY') or '_not yet indexed_'}")
    else:
        st.caption("No variations, extensions, or novations are currently linked to this contract.")

    _render_assessment_group("Commercial, Performance and Renewal Assessment",
                             contract_extraction.COMMERCIAL_ASSESSMENT_FIELDS)

    st.markdown("### Consolidated Procurement Assessment")
    scorecard = contract.get("CLASSIFICATION_SCORECARD") or {}
    if scorecard:
        for key in contract_extraction.CLASSIFICATION_SCORECARD_FIELDS:
            cols = st.columns([2, 3])
            cols[0].markdown(f"**{contract_extraction.CLASSIFICATION_SCORECARD_LABELS[key]}**")
            cols[1].write(scorecard.get(key) or "_Not yet generated._")
    else:
        st.caption("Not yet generated.")

    st.markdown("### Recommended Actions")
    actions = contract.get("RECOMMENDED_ACTIONS") or []
    if actions:
        for action in actions:
            st.markdown(f"- {action}")
    else:
        st.caption("No specific actions flagged.")

with panel_col:
    st.markdown("#### Cited passage")
    active_key = st.session_state.get("active_field")
    if not active_key:
        st.caption("Click **View source** next to any finding to see the exact passage it's drawn from.")
    else:
        active_field = fields.get(active_key)
        if active_field:
            render_citation_panel(session, project, active_field)
