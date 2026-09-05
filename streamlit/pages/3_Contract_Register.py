"""
pages/3_Contract_Register.py — admin view: link a contract's documents
(base + variations/extensions/novations) into one family, run/re-run
extraction, and review/verify every extracted field with its citation.

For the everyday "look up a contract number" experience, see Chat.py
(the Contract Lookup landing page) — this page is for managing the
linking/verification workflow behind it.

LEX-specific: not part of the generic project-llm-wiki template this was
forked from (see contract_linking.py / contract_extraction.py for the
underlying logic — this page is a thin UI over both).
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

import streamlit as st
from snowflake_session import get_session
import contract_linking
import contract_extraction
import contract_output_cache
import required_contracts
from citation_panel_ui import render_citation_panel

st.set_page_config(page_title="Contract Register — LEX", page_icon="📋", layout="wide")

if "project" not in st.session_state:
    st.warning("Select a project on the home page first.")
    st.stop()

session = get_session()
project = st.session_state["project"]

FIELD_LABELS = contract_extraction.FIELD_LABELS

CONFIDENCE_BADGE = {
    "HIGH": "🟢 High",
    "MEDIUM": "🟡 Medium",
    "LOW": "🟠 Low — worth a look",
    "NOT_FOUND": "🔴 Not found in the documents",
    None: "⚪ Not yet extracted",
}

st.title(f"📋 Contract Register — {project.project_name}")
st.caption(
    "Every linked contract gets the same standard questions answered "
    "automatically, each with a citation back to the source document and "
    "a confidence badge. Review and tick **Verified** before relying on a "
    "field — \"not found in the documents\" is a valid, honest answer, "
    "never a guess."
)

# ---------------------------------------------------------------------------
with st.expander("🔗 Link documents to a contract", expanded=False):
    unlinked = contract_linking.list_unlinked_documents(session, project)
    if not unlinked:
        st.info("Every ingested document is linked to a contract.")
    else:
        st.write(
            f"**{len(unlinked)}** ingested document(s) aren't linked to a contract yet — "
            "ingest happens on the Data Sources page; linking (below) is what groups a "
            "base contract with its own variations/extensions/novations. Only contract "
            "numbers already in the Required Contracts Register should normally be used "
            "here — this text box also accepts a brand-new number if genuinely needed."
        )
        required_cw_numbers = [r.cw_number for r in required_contracts.list_required_contracts_status(session, project)]
        existing_families = contract_linking.list_contract_families(session, project)
        existing_cw_numbers = [f.cw_number for f in existing_families]

        OTHER_OPTION = "Other (type below)"
        for doc in unlinked:
            with st.container(border=True):
                cols = st.columns([3, 2, 2, 2, 1])
                cols[0].markdown(f"**{doc['file_name']}**")

                suggested = (doc["suggested_cw_number"] or "").strip().upper()
                choice_options = required_cw_numbers + [OTHER_OPTION]
                default_choice = suggested if suggested in required_cw_numbers else OTHER_OPTION
                cw_choice = cols[1].selectbox(
                    "CW number", choice_options, index=choice_options.index(default_choice),
                    key=f"cwselect_{doc['doc_id']}",
                    help="Prefer a number already in the Required Contracts Register — "
                         "auto-suggested from the file name/text where it matches one.",
                )
                if cw_choice == OTHER_OPTION:
                    cw_number = cols[1].text_input(
                        "Enter CW number", value=suggested, key=f"cw_{doc['doc_id']}"
                    )
                else:
                    cw_number = cw_choice

                doc_role = cols[2].selectbox(
                    "Role", contract_linking.DOC_ROLES, key=f"role_{doc['doc_id']}"
                )
                effective_date = cols[3].date_input(
                    "Effective date", value=None, key=f"date_{doc['doc_id']}"
                )
                if cols[4].button("Link", key=f"link_{doc['doc_id']}", type="primary"):
                    if not cw_number.strip():
                        st.error("Enter a CW number before linking.")
                    else:
                        contract_id = contract_linking.get_or_create_contract(
                            session, project, cw_number, contract_title=None
                        )
                        contract_linking.link_document(
                            session, project, contract_id, doc["doc_id"], doc_role,
                            effective_date=effective_date, linked_by="user",
                        )
                        st.success(f"Linked {doc['file_name']} to {cw_number.strip().upper()} as {doc_role}.")
                        st.rerun()
                if existing_cw_numbers:
                    st.caption(f"Existing contracts: {', '.join(existing_cw_numbers)}")

st.divider()

# ---------------------------------------------------------------------------
families = contract_linking.list_contract_families(session, project)

top_col1, top_col2 = st.columns([3, 1])
top_col1.subheader(f"Contracts ({len(families)})")
if top_col2.button("Run extraction for all contracts", type="primary", disabled=not families):
    with st.spinner(f"Running the standard questions across {len(families)} contract(s)…"):
        results = contract_extraction.extract_stock_fields_for_all_contracts(session, project)
        for touched_contract_id in results:
            contract_output_cache.cache_contract_outputs(session, project, touched_contract_id)
    st.success("Extraction complete.")
    st.rerun()

if not families:
    st.info("No contracts yet — link a document above to create one.")

for family in families:
    with st.expander(f"**{family.cw_number}** — {family.contract_title or '(title not yet set)'} "
                      f"· {family.document_count} document(s) · {family.status}"):
        docs = contract_linking.list_family_documents(session, project, family.contract_id)
        st.markdown("**Linked documents**")
        for d in docs:
            doc_cols = st.columns([3, 2, 2, 1])
            label = f"[{d['FILE_NAME']}]({d['SOURCE_URL']})" if d.get("SOURCE_URL") else d["FILE_NAME"]
            doc_cols[0].markdown(label)
            doc_cols[1].caption(d["DOC_ROLE"])
            doc_cols[2].caption(str(d["EFFECTIVE_DATE"] or ""))
            if doc_cols[3].button("Unlink", key=f"unlink_{family.contract_id}_{d['DOC_ID']}"):
                contract_linking.unlink_document(session, project, family.contract_id, d["DOC_ID"])
                st.rerun()

        extract_col, download_col = st.columns([2, 1])
        if extract_col.button("Run/refresh extraction for this contract",
                              key=f"extract_{family.contract_id}", type="primary"):
            with st.spinner("Running the standard questions…"):
                contract_extraction.extract_stock_fields_for_contract(session, project, family.contract_id)
                contract_output_cache.cache_contract_outputs(session, project, family.contract_id)
            st.success("Extraction complete.")
            st.rerun()

        contract_row = contract_linking.get_contract(session, project, family.contract_id)
        if contract_row:
            # Served from the cache the stage-pickup Task (or the button
            # above) already populated; falls back to building it live if
            # nothing's cached yet — see contract_output_cache.get_or_build_output.
            docx_dl_col, pdf_dl_col = download_col.columns(2)
            docx_dl_col.download_button(
                "⬇ Word",
                data=contract_output_cache.get_or_build_output(session, project, family.contract_id, "docx"),
                file_name=f"{family.cw_number}_Contract_Workspace_Summary.docx",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                key=f"download_docx_{family.contract_id}",
            )
            pdf_dl_col.download_button(
                "⬇ PDF",
                data=contract_output_cache.get_or_build_output(session, project, family.contract_id, "pdf"),
                file_name=f"{family.cw_number}_Contract_Workspace_Summary.pdf",
                mime="application/pdf",
                key=f"download_pdf_{family.contract_id}",
            )

        if contract_row and contract_row.get("OVERVIEW_SUMMARY"):
            st.markdown("**Executive Assessment**")
            st.write(contract_row["OVERVIEW_SUMMARY"])

        fields = {f["FIELD_KEY"]: f for f in contract_extraction.get_contract_fields(session, project, family.contract_id)}

        def _render_field(field_key):
            f = fields[field_key]
            confidence = f.get("CONFIDENCE")
            with st.container(border=True):
                head_cols = st.columns([4, 2])
                head_cols[0].markdown(f"**{FIELD_LABELS.get(field_key, field_key)}**")
                head_cols[1].markdown(CONFIDENCE_BADGE.get(confidence, confidence or "⚪ Not yet extracted"))

                value = f.get("FIELD_VALUE")
                st.write(value if value else "_Not yet extracted — run extraction above._")

                if value and f.get("SOURCE_DOC_ID"):
                    with st.expander("View source"):
                        render_citation_panel(session, project, f)

                if value:
                    verified = st.checkbox(
                        "Verified", value=bool(f.get("IS_VERIFIED")),
                        key=f"verify_{family.contract_id}_{field_key}",
                    )
                    if verified != bool(f.get("IS_VERIFIED")):
                        current_user = session.sql("SELECT CURRENT_USER() AS U").collect()[0]["U"]
                        contract_extraction.set_field_verified(
                            session, project, family.contract_id, field_key,
                            verified, verified_by=current_user,
                        )
                        st.rerun()

        st.markdown("**Contract detail**")
        for key in contract_extraction.CONTRACT_DETAIL_FIELDS:
            _render_field(key)

        st.markdown("**Executive Assessment — findings**")
        for key in contract_extraction.EXECUTIVE_ASSESSMENT_FIELDS:
            _render_field(key)

        st.markdown("**Significant Variations**")
        variations = contract_linking.get_significant_variations(session, project, family.contract_id)
        if variations:
            for v in variations:
                role = v["DOC_ROLE"].replace("_", " ").title()
                date_bit = f", {v['EFFECTIVE_DATE']}" if v.get("EFFECTIVE_DATE") else ""
                st.markdown(f"- **{v['FILE_NAME']}** ({role}{date_bit}): {v.get('NODE_SUMMARY') or '_not yet indexed_'}")
        else:
            st.caption("No variations, extensions, or novations are currently linked to this contract.")

        st.markdown("**Commercial, Performance and Renewal Assessment**")
        for key in contract_extraction.COMMERCIAL_ASSESSMENT_FIELDS:
            _render_field(key)

        st.markdown("**Consolidated Procurement Assessment**")
        scorecard = (contract_row or {}).get("CLASSIFICATION_SCORECARD") or {}
        if scorecard:
            for key in contract_extraction.CLASSIFICATION_SCORECARD_FIELDS:
                cols = st.columns([2, 3])
                cols[0].markdown(f"**{contract_extraction.CLASSIFICATION_SCORECARD_LABELS[key]}**")
                cols[1].write(scorecard.get(key) or "_Not yet generated._")
        else:
            st.caption("Not yet generated.")

        st.markdown("**Recommended Actions**")
        actions = (contract_row or {}).get("RECOMMENDED_ACTIONS") or []
        if actions:
            for action in actions:
                st.markdown(f"- {action}")
        else:
            st.caption("No specific actions flagged.")
