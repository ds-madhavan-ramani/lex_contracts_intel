"""
contract_extraction.py — automatically answer the standard questions for
every contract and persist the results ("History") with citations that can
be traced back to an exact passage in the original document.

The field list below mirrors the team's actual Contract Workspace Summary
Template (assets/Contract_Workspace_Summary_Template.docx) exactly — same
groupings, same row labels — so docx_report.py can drop extracted values
straight into that template's tables without renaming or reshuffling
anything. See CONTRACT_DETAIL_FIELDS / EXECUTIVE_ASSESSMENT_FIELDS /
COMMERCIAL_ASSESSMENT_FIELDS below for how the flat STOCK_FIELDS list maps
onto the template's three tables.

LEX-specific: not part of the generic project-llm-wiki template this was
forked from. Rather than a bespoke single-shot "dump the whole contract and
ask every question at once" prompt, each stock question is answered by
calling query_engine.search() — the same retrieval/citation/synthesis path
— scoped to one contract's linked documents via restrict_to_doc_ids. This
means extraction shares that engine's "say so rather than guessing"
behaviour exactly, with no second engine to keep in sync, and it naturally
covers a contract's variations/extensions since those are all linked
documents in the same family.

Extraction only ever runs on demand (first lookup of a contract, or an
explicit re-run after its documents change) — never on every page view.
The Contract Lookup page reads CONTRACT_FIELD_EXTRACTS ("the History")
directly the rest of the time; see is_extraction_current() for how a
"documents changed since last extraction" prompt is decided.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional

from config import ProjectConfig
import contract_linking
from query_engine import search
from utils.cortex_client import complete, complete_json
from utils.logging_utils import get_logger, log_event

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Contract detail (template table: "Contract detail" / "Current position")
# ---------------------------------------------------------------------------
CONTRACT_DETAIL_FIELDS = ["SUPPLIER", "SERVICES", "COMMENCEMENT", "CURRENT_EXPIRY", "CURRENT_VALUE"]

# ---------------------------------------------------------------------------
# "Executive Assessment" section's table (template: "Assessment area" /
# "Finding and clause/reference")
# ---------------------------------------------------------------------------
EXECUTIVE_ASSESSMENT_FIELDS = [
    "NOVATION_ASSIGNMENT", "CONFIDENTIALITY_DISCLOSURE", "TERM_AND_EXTENSIONS", "COMPLEXITY",
    "SEPARABLE_PORTIONS", "PAYMENT_REGIME", "SECURITY", "DEFECTS_LIABILITY",
]

# ---------------------------------------------------------------------------
# "Commercial, Performance and Renewal Assessment" section's table (same
# "Assessment area" / "Finding and clause/reference" shape, second instance)
# ---------------------------------------------------------------------------
COMMERCIAL_ASSESSMENT_FIELDS = [
    "PRICE_REVIEW", "EA_LABOUR_EXPOSURE", "KPI_FRAMEWORK", "COMMERCIAL_CONSEQUENCES",
    "TERMINATION", "AUTO_RENEWAL_PERPETUAL_TERM", "CHANGE_OF_CONTROL", "CURRENT_STATUS",
]

# The question fed to query_engine.search() for each field, exactly as a
# user's own question would be. Order here is extraction order and display
# order within each group above. Adding a field: append here, add its
# label to FIELD_LABELS, and add its key to whichever *_FIELDS group above
# matches where it belongs in the template — no other code changes needed.
_QUESTIONS = {
    "SUPPLIER": "Who is the supplier / contracted counterparty for this contract?",
    "SERVICES": "In one or two sentences, what services and/or goods does this contract cover?",
    "COMMENCEMENT": "What is the original contract date / commencement date?",
    "CURRENT_EXPIRY": "What is the current contract expiry date?",
    "CURRENT_VALUE": "What is the current contract value?",

    "NOVATION_ASSIGNMENT":
        "Does the novation or assignment clause require consent, and if so from whom? Cite the clause.",
    "CONFIDENTIALITY_DISCLOSURE":
        "What does the confidentiality/disclosure clause require or restrict? Cite the clause.",
    "TERM_AND_EXTENSIONS":
        "What is the contract term, and what extension options exist — how many, for how long, and under "
        "what conditions? Cite the clause.",
    "COMPLEXITY":
        "Describe the complexity of the goods and/or services supplied under this contract.",
    "SEPARABLE_PORTIONS":
        "Does this contract identify separable portions of the work? If so, what are they?",
    "PAYMENT_REGIME":
        "What is the payment regime under this contract — claims process, milestone payments, or something "
        "else — and how does it work?",
    "SECURITY":
        "What securities (e.g. bank guarantees, cash retention) are required under this contract, and in "
        "what amounts?",
    "DEFECTS_LIABILITY":
        "What defects liability provisions does this contract include — period, scope, and remedies?",

    "PRICE_REVIEW": "What price review or price escalation mechanisms does this contract include?",
    "EA_LABOUR_EXPOSURE":
        "What Enterprise Agreement (EA) related clauses or labour-cost exposure, if any, does this contract "
        "include?",
    "KPI_FRAMEWORK": "What KPI or performance-measurement framework does this contract include?",
    "COMMERCIAL_CONSEQUENCES":
        "What commercial consequences — e.g. liquidated damages, rebates, service credits, abatements — "
        "apply for performance failures under this contract?",
    "TERMINATION":
        "What are the termination clauses in this contract, including notice periods and any restrictions "
        "such as perpetual-contract provisions?",
    "AUTO_RENEWAL_PERPETUAL_TERM":
        "Does this contract auto-renew or run in perpetuity? Describe the mechanism, and state whether a "
        "change of ownership or the auto-renewal cycle triggers a right to renegotiate the contract's terms "
        "and conditions.",
    "CHANGE_OF_CONTROL": "What happens under this contract if there is a change of control of either party?",
    "CURRENT_STATUS":
        "What is the current status of this contract (e.g. active and in force, under negotiation, in "
        "dispute, expired)?",
}

STOCK_FIELDS = [
    (key, _QUESTIONS[key])
    for key in CONTRACT_DETAIL_FIELDS + EXECUTIVE_ASSESSMENT_FIELDS + COMMERCIAL_ASSESSMENT_FIELDS
]

# Human-readable label per field — copied verbatim from the template's own
# row labels so the Streamlit pages and the generated document always
# agree with each other and with the template's own wording.
FIELD_LABELS = {
    "SUPPLIER": "Supplier",
    "SERVICES": "Services",
    "COMMENCEMENT": "Original contract / commencement",
    "CURRENT_EXPIRY": "Current expiry",
    "CURRENT_VALUE": "Current value",
    "NOVATION_ASSIGNMENT": "Novation / assignment",
    "CONFIDENTIALITY_DISCLOSURE": "Confidentiality / disclosure",
    "TERM_AND_EXTENSIONS": "Term and extensions",
    "COMPLEXITY": "Complexity",
    "SEPARABLE_PORTIONS": "Separable portions",
    "PAYMENT_REGIME": "Payment regime",
    "SECURITY": "Security",
    "DEFECTS_LIABILITY": "Defects liability",
    "PRICE_REVIEW": "Price review",
    "EA_LABOUR_EXPOSURE": "EA / labour exposure",
    "KPI_FRAMEWORK": "KPI framework",
    "COMMERCIAL_CONSEQUENCES": "Commercial consequences",
    "TERMINATION": "Termination",
    "AUTO_RENEWAL_PERPETUAL_TERM": "Auto-renewal / perpetual term",
    "CHANGE_OF_CONTROL": "Change of control",
    "CURRENT_STATUS": "Current status",
}

# ---------------------------------------------------------------------------
# "Consolidated Procurement Assessment" section's scorecard (template
# table: "Category" / "Assessment") — short-form ratings SYNTHESIZED from
# the detailed fields above (see generate_classification_scorecard), not
# independently searched. Deliberately derived rather than re-derived from
# raw text a second time: this table exists specifically to be a
# consistent roll-up of the detailed findings, and independently
# re-answering "rate the commercial model" from scratch risks disagreeing
# with what the detailed Commercial, Performance and Renewal Assessment
# table already said.
# ---------------------------------------------------------------------------
CLASSIFICATION_SCORECARD_FIELDS = [
    "OVERALL_CLASSIFICATION", "NOVATION_DISCLOSURE_RATING", "COMMERCIAL_MODEL_RATING",
    "OPERATIONAL_EXPOSURE_RATING", "RENEWAL_POSITION_RATING",
]
CLASSIFICATION_SCORECARD_LABELS = {
    "OVERALL_CLASSIFICATION": "Overall classification",
    "NOVATION_DISCLOSURE_RATING": "Novation / disclosure",
    "COMMERCIAL_MODEL_RATING": "Commercial model",
    "OPERATIONAL_EXPOSURE_RATING": "Operational exposure",
    "RENEWAL_POSITION_RATING": "Renewal position",
}

_NOT_FOUND_MARKERS = (
    "i couldn't find a document relevant to that question",
    "no documents have been added to this project yet",
    "haven't been indexed yet",
    "no specific section answers that question",
    "no documents are linked to this contract yet",
)


@dataclass
class FieldExtractResult:
    field_key: str
    confidence: str  # HIGH | MEDIUM | LOW | NOT_FOUND


def _confidence_for(answer_text: str, cited_docs: list) -> str:
    """A simple, honest heuristic, not a model-judged confidence score:
    HIGH when the answer is grounded in at least one citation, NOT_FOUND
    when it matches one of query_engine's own "couldn't find" messages,
    LOW otherwise (grounded in nothing, but not a recognized "not found"
    message either — worth a reviewer's eye). Good enough to sort a review
    queue by; not a substitute for a human actually reading the answer."""
    lowered = (answer_text or "").strip().lower()
    if any(marker in lowered for marker in _NOT_FOUND_MARKERS):
        return "NOT_FOUND"
    if cited_docs:
        return "HIGH"
    return "LOW"


_HIGHLIGHT_PHRASE_PROMPT = """Below is an excerpt from a contract, and an
answer that was derived from it. Quote the single sentence or short phrase
from the excerpt — VERBATIM, character for character — that most directly
supports the answer. This will be used to highlight that exact text for a
reviewer, so it must be copied exactly as written in the excerpt, not
paraphrased, corrected, or reformatted in any way. Return ONLY the quoted
text itself, with no surrounding quotation marks and no commentary.

ANSWER: {answer}

EXCERPT:
{excerpt}
"""


def _normalize_for_match(text: str) -> str:
    """A model-returned "verbatim" quote is only trustworthy for
    highlighting once actually verified against the source — models
    paraphrase small things (capitalization, a rewritten dash, whitespace)
    even when instructed not to. Comparing after collapsing whitespace and
    casing catches the common near-misses without accepting a genuine
    paraphrase."""
    return " ".join((text or "").split()).lower()


def _extract_highlight_phrase(session, project: ProjectConfig, answer_text: str,
                              excerpt_text: str) -> Optional[str]:
    """Best-effort short exact quote from excerpt_text supporting
    answer_text, for the citation viewer to highlight/search for. Returns
    None (not a guess) if the excerpt is too short to bother, the model's
    answer wasn't grounded in anything, or the returned phrase doesn't
    actually verify as a substring of the excerpt — an unverified "quote"
    would be worse than none at all for a feature whose whole point is
    pointing at the exact right text."""
    if not excerpt_text or not answer_text:
        return None
    try:
        phrase = complete(
            session, project.active_model,
            _HIGHLIGHT_PHRASE_PROMPT.format(answer=answer_text[:1000], excerpt=excerpt_text[:4000]),
            max_tokens=300,
        ).strip().strip('"').strip()
    except Exception:  # noqa: BLE001 — highlighting is a nice-to-have, never worth failing extraction over
        logger.warning("EVENT=HIGHLIGHT_PHRASE_FAILED", exc_info=True)
        return None

    if not phrase or _normalize_for_match(phrase) not in _normalize_for_match(excerpt_text):
        return None
    return phrase[:500]


def extract_stock_fields_for_contract(session, project: ProjectConfig, contract_id: int) -> List[FieldExtractResult]:
    """Runs every stock question for one contract, scoped to its linked
    documents, and upserts CONTRACT_FIELD_EXTRACTS — this is what
    populates "the History" the Contract Lookup page reads from. Safe to
    re-run any time — e.g. after a new variation/extension is linked into
    the family, or a linked document's content changed (see
    is_extraction_current) — since each field is a MERGE keyed on
    (CONTRACT_ID, FIELD_KEY), not an append. A field a reviewer already
    marked IS_VERIFIED is overwritten like any other on re-run:
    re-verifying after a contract's documents change is a deliberate
    design choice, not an oversight — an unreviewed re-extraction should
    not silently keep an old field flagged as verified once its source
    documents have changed.

    Also (re)generates the contract's Executive Assessment narrative,
    Recommended Actions, and classification scorecard once every field has
    been extracted — see generate_contract_overview,
    generate_recommended_actions, generate_classification_scorecard.
    """
    schema = project.qualified_schema
    family_doc_ids = contract_linking.get_family_doc_ids(session, project, contract_id)

    results: List[FieldExtractResult] = []
    for field_key, question in STOCK_FIELDS:
        answer = search(session, project, question, use_cache=True,
                        restrict_to_doc_ids=family_doc_ids)
        confidence = _confidence_for(answer.answer, answer.cited_docs)
        top = answer.top_citation
        excerpt = (top or {}).get("excerpt", "")
        highlight_phrase = (
            _extract_highlight_phrase(session, project, answer.answer, excerpt)
            if top else None
        )

        session.sql(
            f"""MERGE INTO {schema}.CONTRACT_FIELD_EXTRACTS AS tgt
                USING (SELECT ? AS CONTRACT_ID, ? AS FIELD_KEY, ? AS FIELD_VALUE,
                              ? AS SOURCE_DOC_ID, ? AS SOURCE_NODE_ID, ? AS SOURCE_QUOTE,
                              ? AS HIGHLIGHT_PHRASE, ? AS CONFIDENCE, ? AS MODEL_USED) AS src
                ON tgt.CONTRACT_ID = src.CONTRACT_ID AND tgt.FIELD_KEY = src.FIELD_KEY
                WHEN MATCHED THEN UPDATE SET
                    FIELD_VALUE = src.FIELD_VALUE, SOURCE_DOC_ID = src.SOURCE_DOC_ID,
                    SOURCE_NODE_ID = src.SOURCE_NODE_ID, SOURCE_QUOTE = src.SOURCE_QUOTE,
                    HIGHLIGHT_PHRASE = src.HIGHLIGHT_PHRASE, CONFIDENCE = src.CONFIDENCE,
                    MODEL_USED = src.MODEL_USED, EXTRACTED_AT = CURRENT_TIMESTAMP(),
                    IS_VERIFIED = FALSE, VERIFIED_BY = NULL, VERIFIED_AT = NULL
                WHEN NOT MATCHED THEN INSERT
                    (CONTRACT_ID, FIELD_KEY, FIELD_VALUE, SOURCE_DOC_ID, SOURCE_NODE_ID,
                     SOURCE_QUOTE, HIGHLIGHT_PHRASE, CONFIDENCE, MODEL_USED)
                    VALUES (src.CONTRACT_ID, src.FIELD_KEY, src.FIELD_VALUE, src.SOURCE_DOC_ID,
                            src.SOURCE_NODE_ID, src.SOURCE_QUOTE, src.HIGHLIGHT_PHRASE,
                            src.CONFIDENCE, src.MODEL_USED)""",
            params=[contract_id, field_key, answer.answer[:4000],
                    top["doc_id"] if top else None, top["node_id"] if top else None,
                    excerpt[:4000] if excerpt else None, highlight_phrase,
                    confidence, project.active_model],
        ).collect()

        results.append(FieldExtractResult(field_key, confidence))

    log_event(logger, "CONTRACT_EXTRACTED", project.project_code,
              contract_id=contract_id,
              not_found=sum(1 for r in results if r.confidence == "NOT_FOUND"))

    generate_contract_overview(session, project, contract_id)
    generate_recommended_actions(session, project, contract_id)
    generate_classification_scorecard(session, project, contract_id)
    return results


def extract_stock_fields_for_all_contracts(session, project: ProjectConfig) -> dict:
    """Runs extract_stock_fields_for_contract for every contract in the
    register — the "Run extraction for all contracts" button, and (at
    Scale-phase volume) what a scheduled Task would call instead of a
    Streamlit click. Returns {contract_id: [FieldExtractResult, ...]}."""
    families = contract_linking.list_contract_families(session, project)
    return {
        family.contract_id: extract_stock_fields_for_contract(session, project, family.contract_id)
        for family in families
    }


def _answered_fields_text(session, project: ProjectConfig, contract_id: int) -> Optional[str]:
    fields = get_contract_fields(session, project, contract_id)
    answered = [f for f in fields if f.get("FIELD_VALUE")]
    if not answered:
        return None
    return "\n\n".join(
        f"{FIELD_LABELS.get(f['FIELD_KEY'], f['FIELD_KEY'])}: {f['FIELD_VALUE']}" for f in answered
    )


_OVERVIEW_PROMPT = """Write a short executive assessment of this contract
(4-6 sentences) for a contracts manager who has never seen it — what it
is, the parties if named, its scope, its term, and anything unusual or
requiring attention (e.g. an unusual termination, novation, or
auto-renewal provision; a short-dated expiry; missing information). Base
it only on the information given below. Write it as plain prose, not a
bulleted list, in a professional, direct tone.

EXTRACTED FIELDS:
{fields_text}
"""


def generate_contract_overview(session, project: ProjectConfig, contract_id: int) -> Optional[str]:
    """Synthesizes CONTRACT_REGISTER.OVERVIEW_SUMMARY — the template's
    "Executive Assessment" narrative — from this contract's already-
    extracted stock fields. Returns None (and leaves OVERVIEW_SUMMARY
    untouched) if no fields have been extracted yet — there's nothing to
    synthesize from."""
    fields_text = _answered_fields_text(session, project, contract_id)
    if not fields_text:
        return None

    overview = complete(session, project.active_model,
                        _OVERVIEW_PROMPT.format(fields_text=fields_text), max_tokens=600)

    schema = project.qualified_schema
    session.sql(
        f"""UPDATE {schema}.CONTRACT_REGISTER
            SET OVERVIEW_SUMMARY = ?, OVERVIEW_GENERATED_AT = CURRENT_TIMESTAMP()
            WHERE CONTRACT_ID = ?""",
        params=[overview[:4000], contract_id],
    ).collect()
    return overview


_RECOMMENDED_ACTIONS_PROMPT = """Based on the extracted contract details
below, list the specific actions a contracts manager should consider
taking on this contract — e.g. confirming a consent requirement before
any assignment, reviewing an approaching expiry or notice deadline,
following up missing information, or flagging an unusual clause for legal
review. Only recommend actions that are actually supported by what's
below — if there's genuinely nothing to flag, return an empty list rather
than inventing generic advice. Each action should be one concise sentence.

EXTRACTED FIELDS:
{fields_text}

Return ONLY JSON: {{"actions": ["...", "..."]}}
"""


def generate_recommended_actions(session, project: ProjectConfig, contract_id: int) -> List[str]:
    """Synthesizes CONTRACT_REGISTER.RECOMMENDED_ACTIONS — the template's
    "Recommended Actions" bullet list — from this contract's already-
    extracted stock fields. Returns [] (and clears RECOMMENDED_ACTIONS) if
    no fields have been extracted yet, or the model genuinely found
    nothing to recommend."""
    schema = project.qualified_schema
    fields_text = _answered_fields_text(session, project, contract_id)
    if not fields_text:
        session.sql(
            f"UPDATE {schema}.CONTRACT_REGISTER SET RECOMMENDED_ACTIONS = NULL WHERE CONTRACT_ID = ?",
            params=[contract_id],
        ).collect()
        return []

    try:
        result = complete_json(session, project.active_model,
                               _RECOMMENDED_ACTIONS_PROMPT.format(fields_text=fields_text),
                               max_tokens=800)
        actions = [a.strip() for a in result.get("actions", []) if a and a.strip()]
    except Exception:  # noqa: BLE001 — a synthesis step failing shouldn't fail the whole extraction run
        logger.warning("EVENT=RECOMMENDED_ACTIONS_FAILED contract_id=%s", contract_id, exc_info=True)
        actions = []

    import json
    session.sql(
        f"""UPDATE {schema}.CONTRACT_REGISTER
            SET RECOMMENDED_ACTIONS = PARSE_JSON(?), OVERVIEW_GENERATED_AT = CURRENT_TIMESTAMP()
            WHERE CONTRACT_ID = ?""",
        params=[json.dumps(actions), contract_id],
    ).collect()
    return actions


_SCORECARD_PROMPT = """Based on the extracted contract assessment below,
provide a short classification (3-8 words each, not a full sentence) for
each of these five categories, consistent with what the detailed
assessment already says — do not introduce a new judgement that
contradicts it:

- overall_classification: overall risk/complexity profile of the contract
- novation_disclosure_rating: novation/disclosure exposure
- commercial_model_rating: the commercial/pricing model
- operational_exposure_rating: operational exposure (KPIs, commercial
  consequences, defects liability)
- renewal_position_rating: renewal/termination/auto-renewal position

EXTRACTED ASSESSMENT:
{fields_text}

Return ONLY JSON: {{"overall_classification": "...", "novation_disclosure_rating": "...",
"commercial_model_rating": "...", "operational_exposure_rating": "...", "renewal_position_rating": "..."}}
"""


def generate_classification_scorecard(session, project: ProjectConfig, contract_id: int) -> Dict[str, str]:
    """Synthesizes CONTRACT_REGISTER.CLASSIFICATION_SCORECARD — the
    template's "Consolidated Procurement Assessment" scorecard table —
    from this contract's already-extracted stock fields. Returns {} (and
    clears CLASSIFICATION_SCORECARD) if no fields have been extracted yet."""
    schema = project.qualified_schema
    fields_text = _answered_fields_text(session, project, contract_id)
    if not fields_text:
        session.sql(
            f"UPDATE {schema}.CONTRACT_REGISTER SET CLASSIFICATION_SCORECARD = NULL WHERE CONTRACT_ID = ?",
            params=[contract_id],
        ).collect()
        return {}

    try:
        result = complete_json(session, project.active_model,
                               _SCORECARD_PROMPT.format(fields_text=fields_text), max_tokens=500)
        scorecard = {
            key.upper(): value.strip()
            for key, value in result.items()
            if key.upper() in CLASSIFICATION_SCORECARD_FIELDS and value and value.strip()
        }
    except Exception:  # noqa: BLE001 — a synthesis step failing shouldn't fail the whole extraction run
        logger.warning("EVENT=CLASSIFICATION_SCORECARD_FAILED contract_id=%s", contract_id, exc_info=True)
        scorecard = {}

    import json
    session.sql(
        f"""UPDATE {schema}.CONTRACT_REGISTER
            SET CLASSIFICATION_SCORECARD = PARSE_JSON(?)
            WHERE CONTRACT_ID = ?""",
        params=[json.dumps(scorecard), contract_id],
    ).collect()
    return scorecard


def get_contract_fields(session, project: ProjectConfig, contract_id: int) -> List[dict]:
    """All extracted fields for one contract, with the source document's
    file name/URL/stage path joined in for display and for the citation
    viewer (which needs SOURCE_STAGE_PATH to build a presigned URL) — the
    Contract Lookup and Contract Register pages both read this directly."""
    schema = project.qualified_schema
    rows = session.sql(
        f"""SELECT CFE.FIELD_KEY, CFE.FIELD_VALUE, CFE.CONFIDENCE, CFE.SOURCE_QUOTE,
                   CFE.HIGHLIGHT_PHRASE, CFE.SOURCE_DOC_ID,
                   CFE.IS_VERIFIED, CFE.VERIFIED_BY, CFE.VERIFIED_AT, CFE.EXTRACTED_AT,
                   RD.FILE_NAME AS SOURCE_FILE_NAME, RD.SOURCE_URL AS SOURCE_URL,
                   RD.STAGE_PATH AS SOURCE_STAGE_PATH
            FROM {schema}.CONTRACT_FIELD_EXTRACTS CFE
            LEFT JOIN {schema}.RAW_DOCUMENTS RD ON CFE.SOURCE_DOC_ID = RD.DOC_ID
            WHERE CFE.CONTRACT_ID = ?""",
        params=[contract_id],
    ).collect()
    by_key = {r["FIELD_KEY"]: dict(r.as_dict()) for r in rows}
    # Always return every field in STOCK_FIELDS order, even if extraction
    # hasn't run yet for this contract — the UI shows "not yet extracted"
    # rather than silently omitting a field.
    return [by_key.get(key, {"FIELD_KEY": key, "FIELD_VALUE": None, "CONFIDENCE": None})
            for key, _ in STOCK_FIELDS]


def is_extraction_current(session, project: ProjectConfig, contract_id: int) -> bool:
    """True if every linked document's latest parse happened at or before
    this contract's last extraction — i.e. nothing has changed since. False
    means a linked document was ingested/updated (its wording, dates, or
    other content changed — see ingestion's SOURCE_HASH-based dedup, which
    only updates PARSED_AT when content actually differs) after extraction
    last ran. The Contract Lookup page uses this to decide whether to show
    a "documents changed — re-run?" prompt instead of silently serving a
    stale answer, and never re-extracts automatically on its own — that
    stays an explicit action, consistent with parsing itself only ever
    happening again when content actually changes, not on every view."""
    schema = project.qualified_schema
    row = session.sql(
        f"""SELECT
              (SELECT MAX(PARSED_AT) FROM {schema}.RAW_DOCUMENTS
                WHERE DOC_ID IN (SELECT DOC_ID FROM {schema}.CONTRACT_DOCUMENT_LINK WHERE CONTRACT_ID = ?)
              ) AS LATEST_PARSE,
              (SELECT MAX(EXTRACTED_AT) FROM {schema}.CONTRACT_FIELD_EXTRACTS
                WHERE CONTRACT_ID = ?
              ) AS LATEST_EXTRACTION""",
        params=[contract_id, contract_id],
    ).collect()[0]
    latest_parse, latest_extraction = row["LATEST_PARSE"], row["LATEST_EXTRACTION"]
    if latest_extraction is None:
        return False
    if latest_parse is None:
        return True  # no linked documents to have changed
    return latest_extraction >= latest_parse


def set_field_verified(session, project: ProjectConfig, contract_id: int, field_key: str,
                       is_verified: bool, verified_by: str) -> None:
    schema = project.qualified_schema
    session.sql(
        f"""UPDATE {schema}.CONTRACT_FIELD_EXTRACTS
            SET IS_VERIFIED = ?, VERIFIED_BY = ?, VERIFIED_AT = CURRENT_TIMESTAMP()
            WHERE CONTRACT_ID = ? AND FIELD_KEY = ?""",
        params=[is_verified, verified_by if is_verified else None, contract_id, field_key],
    ).collect()
