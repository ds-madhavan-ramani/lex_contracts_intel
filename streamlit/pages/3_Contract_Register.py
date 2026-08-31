"""
pages/3_Contract_Register.py — the 15 stock questions, answered and cited,
per contract; and the UI for linking a contract's variations/extensions
into one family.

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

st.set_page_config(page_title="Contract Register — LEX", page_icon="📋", layout="wide")

if "project" not in st.session_state:
    st.warning("Select a project on the home page first.")
    st.stop()

session = get_session()
project = st.session_state["project"]

FIELD_LABELS = {
    "CONTRACT_TITLE": "Contract title",
    "CW_NUMBER": "CW number",
    "CONTRACT_END_DATE": "Contract end date",
    "CONTRACT_SUMMARY": "One-sentence summary",
    "NOVATION_CONSENT": "Novation clause — consent required?",
    "DISCLOSURE_CLAUSE": "Disclosure clause",
    "EXTENSION_OPTIONS": "Extension options",
    "COMPLEXITY_GOODS_SERVICES": "Complexity of goods/services",
    "SEPARABLE_PORTIONS": "Separable portions",
    "PAYMENT_REGIME": "Payment regime",
    "SECURITIES": "Securities",
    "PRICE_REVIEW_MECHANISM": "Price review mechanism",
    "EA_CLAUSES": "EA clauses",
    "TERMINATION_CLAUSE": "Termination clause",
    "AUTO_RENEWAL_MECHANISM": "Auto-renewal mechanism",
}

CONFIDENCE_BADGE = {
    "HIGH": "🟢 High",
    "MEDIUM": "🟡 Medium",
    "LOW": "🟠 Low — worth a look",
    "NOT_FOUND": "🔴 Not found in the documents",
    None: "⚪ Not yet extracted",
}

st.title(f"📋 Contract Register — {project.project_name}")
st.caption(
    "Every linked contract gets the same 15 stock questions answered "
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
            "base contract with its own variations/extensions/novations."
        )
        existing_families = contract_linking.list_contract_families(session, project)
        existing_cw_numbers = [f.cw_number for f in existing_families]

        for doc in unlinked:
            with st.container(border=True):
                cols = st.columns([3, 2, 2, 2, 1])
                cols[0].markdown(f"**{doc['file_name']}**")
                cw_number = cols[1].text_input(
                    "CW number", value=doc["suggested_cw_number"] or "",
                    key=f"cw_{doc['doc_id']}",
                    help="Auto-suggested from the file name/text where possible — "
                         "always double-check before linking.",
                )
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
    with st.spinner(f"Running the 15 stock questions across {len(families)} contract(s)…"):
        contract_extraction.extract_stock_fields_for_all_contracts(session, project)
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

        if st.button("Run/refresh extraction for this contract",
                     key=f"extract_{family.contract_id}", type="primary"):
            with st.spinner("Running the 15 stock questions…"):
                contract_extraction.extract_stock_fields_for_contract(session, project, family.contract_id)
            st.success("Extraction complete.")
            st.rerun()

        st.markdown("**Stock fields**")
        fields = contract_extraction.get_contract_fields(session, project, family.contract_id)
        for f in fields:
            field_key = f["FIELD_KEY"]
            confidence = f.get("CONFIDENCE")
            with st.container(border=True):
                head_cols = st.columns([4, 2])
                head_cols[0].markdown(f"**{FIELD_LABELS.get(field_key, field_key)}**")
                head_cols[1].markdown(CONFIDENCE_BADGE.get(confidence, confidence or "⚪ Not yet extracted"))

                value = f.get("FIELD_VALUE")
                st.write(value if value else "_Not yet extracted — run extraction above._")

                source_name = f.get("SOURCE_FILE_NAME")
                if source_name:
                    source_url = f.get("SOURCE_URL")
                    source_label = f"[{source_name}]({source_url})" if source_url else source_name
                    st.caption(f"Source: {source_label}")

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
