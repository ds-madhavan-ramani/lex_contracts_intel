"""
pages/1_Data_Sources.py — three things: (1) upload/sync the Required
Contracts Register workbook that tells LEX which CW numbers are in scope,
(2) ingest the actual signed/executed contract PDFs (rarely DOCX) for
those contracts, and (3) the manual index-rebuild fallback.

Forked from the project-llm-wiki template (ds-madhavan-ramani/org_mm_chat).
Differences from that template: no register-workbook file-*selection*
logic (org_mm_chat's BIS_ORG_Meeting_Minutes register picked one canonical
file per meeting out of several candidates — not this project's problem);
instead, LEX's own Required Contracts Register (a much simpler workbook —
just a list of CW numbers) drives *which contract numbers exist* in
CONTRACT_REGISTER at all, separately from which documents happen to sit in
a folder. Contract ingestion itself is restricted to PDF/DOCX and flags
files that don't look like a signed/executed copy (see
required_contracts.py) — a soft warning, never a silent drop.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

import streamlit as st
from snowflake_session import get_session
from ingestion.file_ingest import ingest_uploaded_files
from ingestion.sharepoint_ingest import list_sharepoint_folder, ingest_selected_files
from ingestion.index_builder import build_index_for_project
import required_contracts

st.set_page_config(page_title="Data Sources — LEX", page_icon="📁", layout="wide")

if "project" not in st.session_state:
    st.warning("Select a project on the home page first.")
    st.stop()

session = get_session()
project = st.session_state["project"]

st.title(f"📁 Data Sources — {project.project_name}")

tab_register, tab_upload, tab_sharepoint, tab_index = st.tabs(
    ["📋 Required Contracts Register", "📤 Upload Contract Files",
     "🔗 SharePoint / Network Drive", "🌳 Index"]
)

# ---------------------------------------------------------------------------
with tab_register:
    st.subheader("Required Contracts Register")
    st.caption(
        "The .xlsx the team maintains listing every CW number LEX should have data for "
        "(2 today, growing toward 8 for Build/validation, and more later). Upload it here "
        "any time it changes — this is what populates the contract-number list on the "
        "**Contract Lookup** page, independent of which documents happen to be ingested yet."
    )
    register_file = st.file_uploader(
        "Required Contracts Register (.xlsx)", type=["xlsx", "xlsm"], key="register_upload"
    )
    if register_file and st.button("Sync register", type="primary"):
        raw_bytes = register_file.read()
        result = required_contracts.sync_required_contracts_from_xlsx(session, project, raw_bytes)
        if result["added"]:
            st.success(f"Added {len(result['added'])} new contract number(s): {', '.join(result['added'])}")
        if result["already_present"]:
            st.info(f"{len(result['already_present'])} contract number(s) were already in the register.")
        if not result["added"] and not result["already_present"]:
            st.error(
                "No CW numbers found in that workbook. Expected a column headed something like "
                "\"CW Number\" or \"Contract Number\"."
            )
            if result.get("headers_preview"):
                with st.expander("Show the first few rows of each sheet (for troubleshooting)"):
                    for sheet_name, rows in result["headers_preview"].items():
                        st.write(f"**{sheet_name}**")
                        for r_idx, row in enumerate(rows, start=1):
                            st.write(f"- Row {r_idx}: {row}")

    current = required_contracts.list_required_contracts_status(session, project)
    if current:
        st.write(f"**{len(current)}** contract(s) currently in the register:")
        st.dataframe(
            [{"CW Number": r.cw_number, "Title": r.contract_title or "",
              "Linked documents": r.linked_document_count,
              "Fields extracted": r.extracted_field_count}
             for r in current],
            use_container_width=True, hide_index=True,
        )

# ---------------------------------------------------------------------------
with tab_upload:
    st.subheader("Upload contract files")
    st.caption(
        "PDF (or, rarely, DOCX) only — signed/executed copies. A file whose name or text "
        "doesn't contain a marker like \"Signed\" or \"Executed\" is still ingested, just "
        "flagged below so a human can double-check it's the right copy."
    )
    uploaded = st.file_uploader(
        "Choose files", type=["pdf", "docx"], accept_multiple_files=True, key="contract_upload"
    )
    if uploaded and st.button("Ingest uploaded files", type="primary"):
        with st.spinner(f"Ingesting {len(uploaded)} file(s)…"):
            results = ingest_uploaded_files(session, project, uploaded)
            doc_ids = [r.doc_id for r in results if r.status in ("INGESTED", "UPDATED") and r.doc_id]
            indexed_count = 0
            if doc_ids:
                index_result = build_index_for_project(session, project, doc_ids=doc_ids, rebuild=True)
                indexed_count = index_result.indexed
                for err in index_result.errors:
                    st.error(f"⚠️ Indexing failed: {err}")
            # Check the signed/executed marker against the actual parsed
            # text (not the raw file bytes — a compressed PDF's readable
            # text only exists after AI_PARSE_DOCUMENT has run).
            signed_flags = {}
            if doc_ids:
                schema = project.qualified_schema
                placeholders = ", ".join(["?"] * len(doc_ids))
                rows = session.sql(
                    f"SELECT DOC_ID, FILE_NAME, RAW_TEXT FROM {schema}.RAW_DOCUMENTS WHERE DOC_ID IN ({placeholders})",
                    params=doc_ids,
                ).collect()
                signed_flags = {r["DOC_ID"]: required_contracts.looks_signed(r["FILE_NAME"], r["RAW_TEXT"]) for r in rows}
        for r in results:
            fn = st.success if r.status == "INGESTED" else (
                st.info if r.status == "SKIPPED_DUPLICATE" else st.error)
            suffix = ""
            if r.status == "INGESTED" and signed_flags.get(r.doc_id) is False:
                suffix = " ⚠️ no \"Signed\"/\"Executed\" marker found — double-check this is the right copy"
            if r.status == "INGESTED":
                fn(f"✅ {r.file_name} — ingested (doc_id={r.doc_id}){suffix}")
            elif r.status == "SKIPPED_DUPLICATE":
                fn(f"↪️ {r.file_name} — unchanged, skipped")
            else:
                fn(f"❌ {r.file_name} — {r.error}")
        if indexed_count:
            st.success(
                f"Indexed {indexed_count} document(s) — link them to a contract number on the "
                "Contract Register page, then look them up on Contract Lookup."
            )

# ---------------------------------------------------------------------------
with tab_sharepoint:
    st.subheader("Ingest from SharePoint / the network drive")
    default_folder = project.sharepoint_default_folder or ""
    if default_folder:
        st.caption("📁 Source: this project's configured contracts folder")

    with st.expander("Use a different folder instead", expanded=not default_folder):
        override_folder = st.text_input(
            "SharePoint folder URL", placeholder="https://metrotrains.sharepoint.com/:f:/s/.../..."
        )
    folder_url = override_folder.strip() if override_folder.strip() else default_folder

    if "sp_listing" not in st.session_state:
        st.session_state["sp_listing"] = None

    if not folder_url:
        st.warning("This project has no folder configured yet — paste one above.")
    elif st.button("List files in folder"):
        with st.spinner("Listing folder…"):
            try:
                listing = list_sharepoint_folder(session, project, folder_url)
                st.session_state["sp_listing"] = listing
                st.session_state["sp_folder_url"] = folder_url
            except Exception as e:  # noqa: BLE001
                st.error(f"Couldn't list that folder: {e}")
                st.session_state["sp_listing"] = None

    listing = st.session_state.get("sp_listing")
    if listing:
        eligible = [item for item in listing if required_contracts.is_eligible_extension(item.name)]
        st.write(
            f"Found **{len(listing)}** file(s) in this folder — **{len(eligible)}** are PDF/DOCX "
            "(the only formats contracts are ingested from; other files here are hidden)."
        )

        name_filter = st.text_input(
            "Filter by file name", value="",
            help="Matches this text anywhere in the file name (case-insensitive). "
                 "Leave blank to show every PDF/DOCX file in the folder.",
        )
        pool = (
            [item for item in eligible if name_filter.strip().lower() in item.name.lower()]
            if name_filter.strip() else eligible
        )
        if name_filter.strip():
            st.caption(f'Showing {len(pool)} of {len(eligible)} eligible file(s) matching "{name_filter}".')

        default_selected = [item.name for item in pool if required_contracts.looks_signed(item.name)]
        selected_names = st.multiselect(
            "Select files to ingest — 🖊 marks files whose name doesn't show a signed/executed "
            "marker (still selectable, just worth a second look)",
            options=[item.name for item in pool],
            default=default_selected,
            format_func=lambda n: n if required_contracts.looks_signed(n) else f"🖊 {n}",
        )
        if st.button("Ingest selected files", type="primary"):
            selected_items = [i for i in listing if i.name in selected_names]
            with st.spinner(f"Ingesting {len(selected_items)} file(s)…"):
                results = ingest_selected_files(
                    session, project, st.session_state["sp_folder_url"], selected_items
                )
                doc_ids = [r.doc_id for r in results if r.status in ("INGESTED", "UPDATED") and r.doc_id]
                indexed_count = 0
                if doc_ids:
                    index_result = build_index_for_project(session, project, doc_ids=doc_ids, rebuild=True)
                    indexed_count = index_result.indexed
                    for err in index_result.errors:
                        st.error(f"⚠️ Indexing failed: {err}")
                signed_flags = {}
                if doc_ids:
                    schema = project.qualified_schema
                    placeholders = ", ".join(["?"] * len(doc_ids))
                    rows = session.sql(
                        f"SELECT DOC_ID, FILE_NAME, RAW_TEXT FROM {schema}.RAW_DOCUMENTS WHERE DOC_ID IN ({placeholders})",
                        params=doc_ids,
                    ).collect()
                    signed_flags = {r["DOC_ID"]: required_contracts.looks_signed(r["FILE_NAME"], r["RAW_TEXT"]) for r in rows}
            for r in results:
                fn = st.success if r.status in ("INGESTED", "UPDATED") else (
                    st.info if r.status == "SKIPPED_DUPLICATE" else st.error)
                if r.status in ("INGESTED", "UPDATED"):
                    verb = "ingested" if r.status == "INGESTED" else "content changed, index refreshed"
                    suffix = "" if signed_flags.get(r.doc_id, True) else \
                        " ⚠️ no \"Signed\"/\"Executed\" marker found — double-check this is the right copy"
                    fn(f"✅ {r.file_name} — {verb} (doc_id={r.doc_id}){suffix}")
                elif r.status == "SKIPPED_DUPLICATE":
                    fn(f"↪️ {r.file_name} — unchanged, skipped")
                else:
                    fn(f"❌ {r.file_name} — {r.error}")
            if indexed_count:
                st.success(
                    f"Indexed {indexed_count} document(s) — link them to a contract number on "
                    "the Contract Register page, then look them up on Contract Lookup."
                )

# ---------------------------------------------------------------------------
with tab_index:
    st.subheader("Manual index rebuild")
    st.write(
        "Documents are indexed automatically right after they're ingested or "
        "updated — you shouldn't normally need this tab. Use **Rebuild all** "
        "only if the segmentation profile changes, or the index otherwise "
        "needs to be regenerated from scratch. Re-ingesting an unchanged file "
        "never re-parses or re-indexes it — only content that's actually "
        "different triggers this."
    )
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Index new/unindexed documents"):
            with st.spinner("Building index…"):
                result = build_index_for_project(session, project, rebuild=False)
            st.success(f"Indexed {result.indexed} document(s).")
            if result.failed:
                st.error(f"{result.failed} document(s) failed to index:")
                for err in result.errors:
                    st.write(f"- {err}")
    with col2:
        if st.button("Rebuild all", type="secondary"):
            with st.spinner("Rebuilding full index…"):
                result = build_index_for_project(session, project, rebuild=True)
            st.success(f"Rebuilt index for {result.indexed} document(s).")
            if result.failed:
                st.error(f"{result.failed} document(s) failed to index:")
                for err in result.errors:
                    st.write(f"- {err}")
