"""
pages/1_Data_Sources.py — upload contracts directly or ingest them from the
team's SharePoint/network-drive folder.

Forked from the project-llm-wiki template (ds-madhavan-ramani/org_mm_chat).
Difference from that template: no register-workbook file-selection logic
(org_mm_chat's BIS_ORG_Meeting_Minutes register was specific to that
project's meeting-minutes source, not something a contracts library has an
equivalent of) — just list the folder and let the user tick what they want,
same as the template's own stated generic ideal.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

import streamlit as st
from snowflake_session import get_session
from ingestion.file_ingest import ingest_uploaded_files
from ingestion.sharepoint_ingest import list_sharepoint_folder, ingest_selected_files
from ingestion.index_builder import build_index_for_project

st.set_page_config(page_title="Data Sources — LEX", page_icon="📁", layout="wide")

if "project" not in st.session_state:
    st.warning("Select a project on the home page first.")
    st.stop()

session = get_session()
project = st.session_state["project"]

st.title(f"📁 Data Sources — {project.project_name}")
st.caption(
    "Add contracts any time — there's no setup step required before this app "
    "is usable. Upload files directly, or point at the team's SharePoint "
    "folder (the shared network drive) and pick what to bring in. Newly "
    "ingested or updated documents are indexed automatically, right after "
    "ingest. After ingesting a contract's base agreement and any of its "
    "variations/extensions, link them into one contract family on the "
    "**Contract Register** page so both chat and the 15 stock questions "
    "consider the whole history, not just one document."
)


def _reindex(session, project, results) -> int:
    """Indexes/re-indexes exactly the documents this run touched (new or
    changed) — a rebuild for those doc_ids specifically, not the whole
    project, so an unrelated document's index isn't rebuilt on every run.
    Returns the indexed count; any per-document failures are shown inline
    here rather than aborting the rest of the batch."""
    doc_ids = [r.doc_id for r in results if r.status in ("INGESTED", "UPDATED") and r.doc_id]
    if not doc_ids:
        return 0
    result = build_index_for_project(session, project, doc_ids=doc_ids, rebuild=True)
    for err in result.errors:
        st.error(f"⚠️ Indexing failed: {err}")
    return result.indexed


def _status_line(r) -> str:
    if r.status == "INGESTED":
        return f"✅ {r.file_name} — ingested (doc_id={r.doc_id})"
    if r.status == "UPDATED":
        return f"🔄 {r.file_name} — content changed, index refreshed (doc_id={r.doc_id})"
    if r.status == "SKIPPED_DUPLICATE":
        return f"↪️ {r.file_name} — unchanged, skipped"
    return f"❌ {r.file_name} — {r.error}"


tab_upload, tab_sharepoint, tab_index = st.tabs(["📤 Upload Files", "🔗 SharePoint / Network Drive", "🌳 Index"])

# ---------------------------------------------------------------------------
with tab_upload:
    st.subheader("Upload files")
    uploaded = st.file_uploader(
        "Choose files (PDF, DOCX, TXT, XLSX)", type=["pdf", "docx", "txt", "xlsx", "xlsm"],
        accept_multiple_files=True
    )
    if uploaded and st.button("Ingest uploaded files", type="primary"):
        with st.spinner(f"Ingesting {len(uploaded)} file(s)…"):
            results = ingest_uploaded_files(session, project, uploaded)
            indexed_count = _reindex(session, project, results)
        for r in results:
            fn = st.success if r.status == "INGESTED" else (
                st.info if r.status == "SKIPPED_DUPLICATE" else st.error)
            fn(_status_line(r))
        if indexed_count:
            st.success(f"Indexed {indexed_count} document(s) — ready to ask about in Chat.")

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
        st.write(f"Found **{len(listing)}** file(s) in this folder.")

        name_filter = st.text_input(
            "Filter by file name",
            value="",
            help="Matches this text anywhere in the file name (case-insensitive). "
                 "Leave blank to show every file in the folder.",
        )
        pool = (
            [item for item in listing if name_filter.strip().lower() in item.name.lower()]
            if name_filter.strip() else listing
        )
        if name_filter.strip():
            st.caption(f'Showing {len(pool)} of {len(listing)} file(s) matching "{name_filter}".')

        selected_names = st.multiselect(
            "Select files to ingest",
            options=[item.name for item in pool],
            default=[item.name for item in pool],
        )
        if st.button("Ingest selected files", type="primary"):
            selected_items = [i for i in listing if i.name in selected_names]
            with st.spinner(f"Ingesting {len(selected_items)} file(s)…"):
                results = ingest_selected_files(
                    session, project, st.session_state["sp_folder_url"], selected_items
                )
                indexed_count = _reindex(session, project, results)
            for r in results:
                fn = st.success if r.status in ("INGESTED", "UPDATED") else (
                    st.info if r.status == "SKIPPED_DUPLICATE" else st.error)
                fn(_status_line(r))
            if indexed_count:
                st.success(
                    f"Indexed {indexed_count} document(s) — ready to ask about in Chat, "
                    "and to link into a contract family on the Contract Register page."
                )

# ---------------------------------------------------------------------------
with tab_index:
    st.subheader("Manual index rebuild")
    st.write(
        "Documents are indexed automatically right after they're ingested "
        "or updated — you shouldn't normally need this tab. Use **Rebuild "
        "all** only if the segmentation profile changes, or the index "
        "otherwise needs to be regenerated from scratch."
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
