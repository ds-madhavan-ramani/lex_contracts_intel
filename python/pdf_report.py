"""
pdf_report.py — generate a PDF version of the Contract Workspace Summary
for one contract, built directly from the same extracted data
docx_report.py uses (CONTRACT_FIELD_EXTRACTS / CONTRACT_REGISTER) — not by
converting the .docx file to PDF. There is no LibreOffice, Word, or any
other docx-to-PDF converter available in either the Streamlit container
runtime or a Snowflake stored procedure (see streamlit/pyproject.toml's
own note on why python-docx was chosen specifically for having no system
dependencies), so this module renders the same content independently with
reportlab — also pure Python, no system dependencies — rather than
converting the Word file. Content matches docx_report.py section-for-
section and field-for-field; visual styling is reportlab's own layout,
not a pixel copy of the Word template.

LEX-specific: not part of the generic project-llm-wiki template.
"""

import io
from typing import List, Optional, Tuple

import contract_extraction
import contract_linking
from config import ProjectConfig

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (
    ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

_DISCLAIMER = (
    "Note: This summary was generated using an AI tool from the contract documents linked "
    "to this CW number in Snowflake. Every finding above is grounded in the cited source "
    "document and section — verify any field not yet marked Verified before relying on it, "
    "particularly where the confidence shown is Low or Not found."
)
_NO_VARIATIONS_TEXT = "No variations, extensions, or novations are currently linked to this contract."
_NO_ACTIONS_TEXT = "No specific actions flagged."
_NO_RISKS_TEXT = "No significant commercial risks flagged."

_TABLE_STYLE = TableStyle([
    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2f3e4e")),
    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ("FONTNAME", (0, 1), (0, -1), "Helvetica-Bold"),
    ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f5f5")]),
    ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ("TOPPADDING", (0, 0), (-1, -1), 4),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
])


def _page_header_footer(cw_number: str, title: str, label: str):
    """Returns a reportlab onFirstPage/onLaterPages callback that repeats
    the contract's identity and which of the two output lengths this is
    on every page. SimpleDocTemplate's flowable content has no built-in
    running header/footer concept — a page-canvas callback is reportlab's
    documented mechanism for drawing the same thing on every page
    independent of the flowable layout, matching what
    docx_report._set_header_footer does for the Word outputs."""
    header_text = f"{cw_number} | {title}" if title else cw_number
    footer_text = f"{cw_number} — {label}"

    def _draw(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#666666"))
        page_width, page_height = doc.pagesize
        canvas.drawString(1.8 * cm, page_height - 1.1 * cm, header_text[:110])
        canvas.drawString(1.8 * cm, 1.0 * cm, footer_text)
        canvas.drawRightString(page_width - 1.8 * cm, 1.0 * cm, f"Page {doc.page}")
        canvas.restoreState()

    return _draw


def _styles():
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle("LexCellText", parent=styles["BodyText"], fontSize=9, leading=12))
    styles.add(ParagraphStyle("LexH1", parent=styles["Heading1"], spaceBefore=14, spaceAfter=6))
    styles.add(ParagraphStyle("LexH2", parent=styles["Heading2"], spaceBefore=10, spaceAfter=4))
    styles.add(ParagraphStyle("LexNote", parent=styles["LexCellText"], textColor=colors.grey, spaceBefore=10))
    return styles


def _cell(styles, text: Optional[str]) -> Paragraph:
    safe = (text or "Not yet extracted.").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return Paragraph(safe.replace("\n", "<br/>"), styles["LexCellText"])


def _kv_table(styles, header: Tuple[str, str], rows: List[Tuple[str, Optional[str]]]) -> Table:
    data = [[_cell(styles, header[0]), _cell(styles, header[1])]] + [
        [_cell(styles, k), _cell(styles, v)] for k, v in rows
    ]
    table = Table(data, colWidths=[5.5 * cm, 11 * cm], repeatRows=1)
    table.setStyle(_TABLE_STYLE)
    return table


def _bullets(styles, items: List[str], empty_text: str) -> ListFlowable:
    display_items = items or [empty_text]
    return ListFlowable(
        [ListItem(_cell(styles, item)) for item in display_items],
        bulletType="bullet", leftIndent=14,
    )


def build_contract_pdf(session, project: ProjectConfig, contract_id: int) -> bytes:
    """Builds the Contract Workspace Summary as a PDF for one contract,
    from the same already-extracted data docx_report.build_contract_docx
    uses — no new Cortex calls here, purely formatting/layout. Returns raw
    PDF bytes for st.download_button."""
    contract = contract_linking.get_contract(session, project, contract_id)
    fields = {f["FIELD_KEY"]: f for f in contract_extraction.get_contract_fields(session, project, contract_id)}
    variations = contract_linking.get_significant_variations(session, project, contract_id)

    def value_of(field_key: str) -> Optional[str]:
        return fields.get(field_key, {}).get("FIELD_VALUE")

    styles = _styles()
    story = []

    title_text = (
        contract.get("CONTRACT_TITLE")
        or f"{value_of('SUPPLIER') or contract['CW_NUMBER']} - {value_of('SERVICES') or contract['CW_NUMBER']}"
    )
    story.append(Paragraph(title_text, styles["Title"]))
    story.append(Paragraph("Contract Review Summary and Assessment", styles["Normal"]))
    story.append(Spacer(1, 0.4 * cm))

    story.append(_kv_table(styles, ("Contract detail", "Current position"), [
        (contract_extraction.FIELD_LABELS[key], value_of(key))
        for key in contract_extraction.CONTRACT_DETAIL_FIELDS
    ]))

    story.append(Paragraph("Executive Assessment", styles["LexH1"]))
    story.append(_cell(styles, contract.get("OVERVIEW_SUMMARY") or "Not yet generated."))
    story.append(Spacer(1, 0.3 * cm))
    story.append(_kv_table(styles, ("Assessment area", "Finding and clause/reference"), [
        (contract_extraction.FIELD_LABELS[key], value_of(key))
        for key in contract_extraction.EXECUTIVE_ASSESSMENT_FIELDS
    ]))

    story.append(Paragraph("Commercial, Performance and Renewal Assessment", styles["LexH1"]))
    story.append(_kv_table(styles, ("Assessment area", "Finding and clause/reference"), [
        (contract_extraction.FIELD_LABELS[key], value_of(key))
        for key in contract_extraction.COMMERCIAL_ASSESSMENT_FIELDS
    ]))

    story.append(Paragraph("Significant Variations", styles["LexH2"]))
    variation_lines = []
    for v in variations:
        label = f"{v['FILE_NAME']} ({v['DOC_ROLE'].replace('_', ' ').title()}"
        label += f", {v['EFFECTIVE_DATE']})" if v.get("EFFECTIVE_DATE") else ")"
        summary = v.get("NODE_SUMMARY") or "not yet indexed"
        variation_lines.append(f"{label}: {summary}")
    story.append(_bullets(styles, variation_lines, _NO_VARIATIONS_TEXT))

    story.append(Paragraph("Consolidated Procurement Assessment", styles["LexH2"]))
    scorecard = contract.get("CLASSIFICATION_SCORECARD") or {}
    story.append(_kv_table(styles, ("Category", "Assessment"), [
        (contract_extraction.CLASSIFICATION_SCORECARD_LABELS[key], scorecard.get(key))
        for key in contract_extraction.CLASSIFICATION_SCORECARD_FIELDS
    ]))

    strategy = contract.get("PROCUREMENT_STRATEGY") or {}
    story.append(Paragraph("Key Commercial Risks", styles["LexH2"]))
    story.append(_bullets(styles, strategy.get("KEY_COMMERCIAL_RISKS") or [], _NO_RISKS_TEXT))

    story.append(Paragraph("Procurement Recommendation", styles["LexH2"]))
    story.append(_cell(styles, strategy.get("PROCUREMENT_RECOMMENDATION") or "Not yet generated."))

    story.append(Paragraph("Retender Strategy", styles["LexH2"]))
    story.append(_cell(styles, strategy.get("RETENDER_STRATEGY") or "Not yet generated."))

    story.append(Paragraph("Recommended Actions", styles["LexH2"]))
    story.append(_bullets(styles, contract.get("RECOMMENDED_ACTIONS") or [], _NO_ACTIONS_TEXT))

    story.append(Paragraph(_DISCLAIMER, styles["LexNote"]))

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=1.8 * cm, rightMargin=1.8 * cm, topMargin=1.5 * cm, bottomMargin=1.5 * cm,
        title=title_text,
    )
    header_footer = _page_header_footer(contract["CW_NUMBER"], title_text, "Detailed Summary")
    doc.build(story, onFirstPage=header_footer, onLaterPages=header_footer)
    return buffer.getvalue()


def build_contract_pdf_condensed(session, project: ProjectConfig, contract_id: int) -> bytes:
    """Builds a short, ~2-page condensed Contract Review Summary — the
    same section order as build_contract_pdf above, but using
    CONTRACT_REGISTER.CONDENSED_FIELDS (see
    contract_extraction.generate_condensed_summary) in place of each
    field's full paragraph-length FIELD_VALUE, and capping Recommended
    Actions to its top 5 items — in the same spirit as the CoPilot-
    generated reference summary CW20841 was benchmarked against. No new
    Cortex calls here, purely formatting."""
    contract = contract_linking.get_contract(session, project, contract_id)
    condensed = contract.get("CONDENSED_FIELDS") or {}
    fields = {f["FIELD_KEY"]: f for f in contract_extraction.get_contract_fields(session, project, contract_id)}
    variations = contract_linking.get_significant_variations(session, project, contract_id)

    def condensed_value_of(field_key: str) -> Optional[str]:
        return condensed.get(field_key) or fields.get(field_key, {}).get("FIELD_VALUE")

    styles = _styles()
    story = []

    title_text = (
        contract.get("CONTRACT_TITLE")
        or f"{condensed_value_of('SUPPLIER') or contract['CW_NUMBER']} - "
           f"{condensed_value_of('SERVICES') or contract['CW_NUMBER']}"
    )
    story.append(Paragraph(title_text, styles["Title"]))
    story.append(Paragraph("Contract Review Summary and Assessment — Condensed", styles["Normal"]))
    story.append(Spacer(1, 0.4 * cm))

    story.append(_kv_table(styles, ("Contract detail", "Current position"), [
        (contract_extraction.FIELD_LABELS[key], condensed_value_of(key))
        for key in contract_extraction.CONTRACT_DETAIL_FIELDS
    ]))

    story.append(Paragraph("Executive Assessment", styles["LexH1"]))
    story.append(_cell(styles, contract.get("OVERVIEW_SUMMARY") or "Not yet generated."))
    story.append(Spacer(1, 0.3 * cm))
    story.append(_kv_table(styles, ("Assessment area", "Finding"), [
        (contract_extraction.FIELD_LABELS[key], condensed_value_of(key))
        for key in contract_extraction.EXECUTIVE_ASSESSMENT_FIELDS
    ]))

    story.append(Paragraph("Commercial, Performance and Renewal Assessment", styles["LexH1"]))
    story.append(_kv_table(styles, ("Assessment area", "Finding"), [
        (contract_extraction.FIELD_LABELS[key], condensed_value_of(key))
        for key in contract_extraction.COMMERCIAL_ASSESSMENT_FIELDS
    ]))

    story.append(Paragraph("Significant Variations", styles["LexH2"]))
    variation_lines = []
    for v in variations:
        label = f"{v['FILE_NAME']} ({v['DOC_ROLE'].replace('_', ' ').title()})"
        summary = v.get("NODE_SUMMARY") or "not yet indexed"
        summary = summary if len(summary) <= 160 else summary[:157] + "..."
        variation_lines.append(f"{label}: {summary}")
    story.append(_bullets(styles, variation_lines, _NO_VARIATIONS_TEXT))

    story.append(Paragraph("Consolidated Procurement Assessment", styles["LexH2"]))
    scorecard = contract.get("CLASSIFICATION_SCORECARD") or {}
    story.append(_kv_table(styles, ("Category", "Assessment"), [
        (contract_extraction.CLASSIFICATION_SCORECARD_LABELS[key], scorecard.get(key))
        for key in contract_extraction.CLASSIFICATION_SCORECARD_FIELDS
    ]))

    strategy = contract.get("PROCUREMENT_STRATEGY") or {}
    story.append(Paragraph("Key Commercial Risks", styles["LexH2"]))
    # Capped like Recommended Actions below -- generate_procurement_strategy's
    # own prompt asks for 3-6, ranked most severe first, so the top 4 keeps
    # the most important ones within this format's page budget.
    story.append(_bullets(styles, (strategy.get("KEY_COMMERCIAL_RISKS") or [])[:4], _NO_RISKS_TEXT))

    story.append(Paragraph("Procurement Recommendation", styles["LexH2"]))
    story.append(_cell(styles, strategy.get("PROCUREMENT_RECOMMENDATION") or "Not yet generated."))

    story.append(Paragraph("Retender Strategy", styles["LexH2"]))
    story.append(_cell(styles, strategy.get("RETENDER_STRATEGY") or "Not yet generated."))

    story.append(Paragraph("Recommended Actions", styles["LexH2"]))
    top_actions = (contract.get("RECOMMENDED_ACTIONS") or [])[:5]
    story.append(_bullets(styles, top_actions, _NO_ACTIONS_TEXT))

    story.append(Paragraph(_DISCLAIMER, styles["LexNote"]))

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=1.8 * cm, rightMargin=1.8 * cm, topMargin=1.5 * cm, bottomMargin=1.5 * cm,
        title=title_text,
    )
    header_footer = _page_header_footer(contract["CW_NUMBER"], title_text, "Condensed Summary")
    doc.build(story, onFirstPage=header_footer, onLaterPages=header_footer)
    return buffer.getvalue()
