#!/usr/bin/env python3
"""
pipeline/network_drive_browser_app.py — standalone Streamlit UI for the
Linux bridge host (see network_drive_to_stage.py's module docstring for
why this runs outside Snowflake): browse a folder on LEX's network drive,
see which PDFs look like signed/executed contracts, pick which to copy,
and confirm what has actually landed in the Snowflake stage.

This is a front end over network_drive_to_stage.py's own functions (same
Snowflake connection/config loading, same PUT-to-stage logic) — no second
implementation of that plumbing.

Run on the bridge host, with the same environment variables documented in
network_drive_to_stage.py's docstring (SNOWFLAKE_*, NETWORK_DRIVE_*):

    pip install streamlit
    streamlit run pipeline/network_drive_browser_app.py

UNVERIFIED: written without a live SMB server or Snowflake account to test
against — treat this as a best-effort starting point.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

import streamlit as st

from required_contracts import looks_signed  # noqa: E402
from utils.network_drive_client import iter_files, NetworkDriveError  # noqa: E402

from network_drive_to_stage import (  # noqa: E402
    INBOX_STAGE,
    get_snowflake_connection,
    load_drive_config,
    ensure_inbox_stage,
    _stage_one,
)

# Initial scope, per the team: only search these 7 CW folders rather than
# the full Ariba\Contracts tree (100+ CW folders, each many levels deep —
# walking all of them took long enough that the UI looked hung). Editable
# in the UI below; this is just the starting default.
DEFAULT_CW_FOLDERS = [
    "CW14465", "CW20841", "CW23401", "CW67873",
    "CW82416", "CW86868", "CW87278",
]


@st.cache_data(ttl=300)
def _contract_titles() -> dict:
    """CW number -> title, from the Required Contracts Register already
    synced into CONTRACT_REGISTER. Best-effort: an empty dict here just
    means titles won't show, not a hard failure — the register may not be
    synced yet, or may not have a title for every CW number."""
    try:
        cur = conn.cursor()
        cur.execute("SELECT CW_NUMBER, CONTRACT_TITLE FROM MEDSCOMA.DATA_LEX.CONTRACT_REGISTER")
        return {row[0]: row[1] for row in cur.fetchall() if row[1]}
    except Exception:
        return {}

st.set_page_config(page_title="LEX Network Drive Bridge", page_icon="📁", layout="wide")
st.title("📁 LEX Network Drive Bridge")
st.caption(
    "Stopgap tool for while Snowflake can't reach the network drive directly — "
    "runs here on the bridge host, stages files into "
    f"@{INBOX_STAGE} for the app to pick up later."
)


@st.cache_resource
def _connection():
    return get_snowflake_connection()


@st.cache_resource
def _drive_config():
    conn = _connection()
    ensure_inbox_stage(conn)
    return load_drive_config(conn)


try:
    conn = _connection()
    drive = _drive_config()
except SystemExit as e:
    st.error(str(e))
    st.stop()

st.caption(f"Source: \\\\{drive.network_drive_host}\\{drive.network_drive_share}")

default_path = drive.network_drive_default_path or ""
base_path = st.text_input(
    "Base path (relative to the share root)",
    value=default_path,
    placeholder=r"e.g. AppData\prd\MR5Documents\Ariba",
)
cw_text = st.text_area(
    "CW folders to search (one per line) — each is searched as "
    f"{(base_path or '<base path>').strip(chr(92))}\\Contracts\\<CW number>",
    value="\n".join(DEFAULT_CW_FOLDERS),
    height=150,
)
show_all_pdfs = st.checkbox(
    "Show all PDFs in these folders (not just ones marked Signed/Executed)"
)

if "browser_rows" not in st.session_state:
    st.session_state["browser_rows"] = []
if "browser_items" not in st.session_state:
    st.session_state["browser_items"] = []

if st.button("List files of interest", type="primary"):
    cw_folders = [line.strip() for line in cw_text.splitlines() if line.strip()]
    titles = _contract_titles()
    items, rows = [], []
    status = st.empty()
    table_placeholder = st.empty()

    for cw in cw_folders:
        status.write(f"Searching {cw}… ({len(items)} matching file(s) so far)")
        relative_path = f"{base_path.strip(chr(92) + '/')}\\Contracts\\{cw}"
        try:
            for item in iter_files(drive, relative_path=relative_path, recursive=True):
                if not item.name.lower().endswith(".pdf"):
                    continue
                if not show_all_pdfs and not looks_signed(item.name):
                    continue
                items.append(item)
                rows.append({
                    "CW/Folder": cw,
                    "Contract Title": titles.get(cw, ""),
                    "File": item.name,
                })
                # Update the table every few files rather than only once
                # everything is done — a folder with hundreds of nested
                # subfolders can take a while to walk in full.
                if len(items) % 3 == 0:
                    table_placeholder.dataframe(rows, use_container_width=True, hide_index=True)
        except NetworkDriveError as e:
            st.warning(f"{cw}: {e}")

    table_placeholder.dataframe(rows, use_container_width=True, hide_index=True)
    status.write(f"Done — {len(items)} matching file(s) found across {len(cw_folders)} CW folder(s).")
    st.session_state["browser_items"] = items
    st.session_state["browser_rows"] = rows

items = st.session_state["browser_items"]
rows = st.session_state["browser_rows"]
if items:
    # Keyed by index, not file name — two different CW folders can have
    # identically-named files (e.g. every CW folder having its own
    # "Executed.pdf").
    labels = [f"{r['CW/Folder']} — {r['File']}" for r in rows]
    selected_labels = st.multiselect(
        "Select files to copy to the stage", options=labels, default=labels
    )
    if st.button("Copy selected files to stage"):
        selected_items = [item for item, label in zip(items, labels) if label in selected_labels]
        progress = st.empty()
        for item in selected_items:
            progress.write(f"Staging {item.name}…")
            _stage_one(conn, drive, item.item_id, item.name)
        progress.write("Done.")
        st.session_state["staged_just_now"] = [i.name for i in selected_items]
        st.rerun()

st.divider()
st.subheader("Stage folder contents")
st.caption(f"@{INBOX_STAGE} — what has actually been copied so far.")
if st.button("Refresh stage listing"):
    st.session_state.pop("stage_listing_cache", None)

if "stage_listing_cache" not in st.session_state:
    cur = conn.cursor()
    cur.execute(f"LIST @{INBOX_STAGE}")
    st.session_state["stage_listing_cache"] = cur.fetchall()

staged_rows = st.session_state["stage_listing_cache"]
just_staged = set(st.session_state.pop("staged_just_now", []))
if staged_rows:
    st.dataframe(
        [
            {
                "File": row[0].rsplit("/", 1)[-1],
                "Size (bytes)": row[1],
                "Just copied": "✅" if row[0].rsplit("/", 1)[-1] in just_staged else "",
            }
            for row in staged_rows
        ],
        use_container_width=True,
        hide_index=True,
    )
else:
    st.write("Stage is empty.")
