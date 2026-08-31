"""
citation_panel_ui.py — the "cited passage" side panel shared by the
Contract Lookup and Contract Register pages: an exact, format-agnostic
highlighted excerpt (always available, built from text stored verbatim at
extraction time) plus, for PDFs, a best-effort rendered-and-highlighted
view of the original document (see citation_viewer.py for what
"best-effort" means there, and its caveats).

LEX-specific: not part of the generic project-llm-wiki template.
"""

import html as _html

import streamlit as st

import citation_viewer


def render_citation_panel(session, project, field: dict) -> None:
    """field is one entry from contract_extraction.get_contract_fields()."""
    quote = field.get("SOURCE_QUOTE")
    phrase = field.get("HIGHLIGHT_PHRASE")
    file_name = field.get("SOURCE_FILE_NAME")
    stage_path = field.get("SOURCE_STAGE_PATH")

    if not quote:
        st.caption("No cited passage recorded for this answer.")
        return

    st.markdown(f"**Cited passage** — {file_name or 'unknown source'}")
    if phrase and phrase.lower() in quote.lower():
        # Case-insensitive highlight of the exact phrase within the exact
        # quote — both stored verbatim from the source at extraction time,
        # so this never needs the fuzzy matching the PDF viewer below does.
        idx = quote.lower().index(phrase.lower())
        before, matched, after = quote[:idx], quote[idx:idx + len(phrase)], quote[idx + len(phrase):]
        body = (
            f"{_html.escape(before)}<mark>{_html.escape(matched)}</mark>{_html.escape(after)}"
        )
    else:
        body = _html.escape(quote)
    st.markdown(
        f"<div style='white-space:pre-wrap; font-size:0.92rem; line-height:1.5;'>{body}</div>",
        unsafe_allow_html=True,
    )

    if not file_name or not stage_path:
        return

    st.divider()
    url = citation_viewer.get_presigned_url(session, project, stage_path)
    if citation_viewer.is_pdf(file_name):
        st.caption("Original document — best-effort highlight, see citation_viewer.py for caveats")
        if url:
            st.components.v1.html(
                citation_viewer.build_pdf_viewer_html(url, phrase), height=640, scrolling=False
            )
        else:
            st.warning("Couldn't generate a link to the original document.")
    else:
        st.caption("Inline preview is PDF-only for now — open the original instead.")
        if url:
            st.link_button("Open original document ↗", url)
            if phrase:
                st.caption(f"Search for: “{phrase}”")
        else:
            st.warning("Couldn't generate a link to the original document.")
