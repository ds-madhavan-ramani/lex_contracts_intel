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
forked from. Every field is answered by one of FIELD_GROUPS's small
"agents" (2-3 closely related fields each, 10 agents total) — one Cortex
call per agent, each reading every one of the contract's linked documents
in FULL (base + every variation/extension/novation in the family), not
narrow per-question retrieval. CONFIRMED on a live account: an earlier
design here called query_engine.search() once per field, scoped to the
family via restrict_to_doc_ids — sharing that engine's retrieval/
reranking pipeline — and it kept returning "the excerpts provided do not
contain sufficient information" for most fields on real, densely-written
contracts, because that pipeline wasn't reliably surfacing the right
section for a given question even when the answer was genuinely in the
documents. Reading the full family directly closes that gap entirely for
a workload where the total text (a handful of PDFs) comfortably fits in
one call. Fields are kept in small clusters rather than one call per
whole template section (an earlier version of this design) because
CONFIRMED on a live account: cramming 5-8 unrelated questions into one
JSON response measurably hurt per-field quote precision — the model
would name the wrong document number for an otherwise-genuine verbatim
quote when juggling that many questions over a large multi-document
context at once, which _extract_field_group used to treat as
"unverified" and downgrade to LOW confidence purely because of that
misattribution, not because the underlying answer was actually weak. See
extract_stock_fields_for_contract and _extract_field_group — the latter
now double-checks a quote against every document in the family, not just
the one the model named, before giving up on it.

These are still run one agent at a time, not concurrently: Snowpark's
Session/connection isn't documented as safe for concurrent statement
execution from multiple threads, and this app only has the one session
Streamlit-in-Snowflake hands it — there's no straightforward way to open
several independent Snowflake connections from inside this app to safely
parallelize across. Ten sequential full-family reads (vs. three
previously) costs roughly 3x more input tokens and wall time per
extraction run; see FIELD_GROUPS for the tradeoff this makes instead
(narrower focus per call over raw parallelism).

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

# The question put to the relevant group's "agent" call for each field
# (see FIELD_GROUPS / _extract_field_group), exactly as a user's own
# question would be. Order here is extraction order and display
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
        "Describe the complexity of the goods and/or services supplied under this contract, then state a "
        "single decisive Complexity Rating of Low, Medium, or High — never a hedge between two levels — "
        "based on factors such as safety-criticality, 24/7 or network-wide delivery, number of distinct "
        "service streams, and regulatory/compliance obligations.",
    "SEPARABLE_PORTIONS":
        "For procurement purposes, could any part of the services be separately tendered, terminated, or "
        "transferred without affecting the rest? Answer based on how the services are actually delivered as "
        "an operating model, not merely how the scope-of-work document happens to be organized into "
        "schedules or sections — if the services are delivered as one integrated model with no such "
        "separable portions, say so explicitly rather than listing document sections.",
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

# field_key -> which of the template's tables it belongs to — used by
# build_fields_table's "Section" column, matching the same grouping the
# per-section UI (Chat.py / Contract Register) already renders separately.
SECTION_FOR_FIELD = {
    **{key: "Contract detail" for key in CONTRACT_DETAIL_FIELDS},
    **{key: "Executive Assessment" for key in EXECUTIVE_ASSESSMENT_FIELDS},
    **{key: "Commercial, Performance and Renewal Assessment" for key in COMMERCIAL_ASSESSMENT_FIELDS},
}

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

# ---------------------------------------------------------------------------
# Forward-looking procurement wrap-up, rendered after the Consolidated
# Procurement Assessment scorecard — matching the CoPilot-generated
# reference summary CW20841 was benchmarked against, which closes with
# exactly this kind of strategic wrap-up ("Key Commercial Risks" /
# "Procurement Recommendation" / a retender-vs-renegotiate assessment).
# SYNTHESIZED from the detailed fields (see generate_procurement_strategy),
# same reasoning as CLASSIFICATION_SCORECARD_FIELDS above: a roll-up of
# findings already made, not independently re-derived from raw text.
# ---------------------------------------------------------------------------
PROCUREMENT_STRATEGY_FIELDS = ["KEY_COMMERCIAL_RISKS", "PROCUREMENT_RECOMMENDATION", "RETENDER_STRATEGY"]
PROCUREMENT_STRATEGY_LABELS = {
    "KEY_COMMERCIAL_RISKS": "Key Commercial Risks",
    "PROCUREMENT_RECOMMENDATION": "Procurement Recommendation",
    "RETENDER_STRATEGY": "Retender Strategy",
}

@dataclass
class FieldExtractResult:
    field_key: str
    confidence: str  # HIGH | MEDIUM | LOW | NOT_FOUND


# One "agent" per group — each reads every one of the contract's linked
# documents in full and answers just its own group's 2-3 questions. Ten
# focused calls per contract: enough to keep each call's attention on a
# small, closely-related cluster (sharper quotes, per _extract_field_group's
# own docstring) without going all the way to one call per field (21 full
# rereads of the same document family would multiply cost further for
# fields with little left to disambiguate, e.g. SUPPLIER/SERVICES). This
# grouping is independent of SECTION_FOR_FIELD/CONTRACT_DETAIL_FIELDS/etc.
# above, which is about which template TABLE a field's UI row renders
# in — an extraction agent's boundary and a template table's boundary
# don't need to be the same thing.
FIELD_GROUPS = [
    ("Parties & services", ["SUPPLIER", "SERVICES"]),
    ("Dates & value", ["COMMENCEMENT", "CURRENT_EXPIRY", "CURRENT_VALUE"]),
    ("Novation & confidentiality", ["NOVATION_ASSIGNMENT", "CONFIDENTIALITY_DISCLOSURE"]),
    ("Term & complexity", ["TERM_AND_EXTENSIONS", "COMPLEXITY"]),
    ("Scope & payment", ["SEPARABLE_PORTIONS", "PAYMENT_REGIME"]),
    ("Security & defects", ["SECURITY", "DEFECTS_LIABILITY"]),
    ("Pricing & labour", ["PRICE_REVIEW", "EA_LABOUR_EXPOSURE"]),
    ("Performance & consequences", ["KPI_FRAMEWORK", "COMMERCIAL_CONSEQUENCES"]),
    ("Termination & renewal", ["TERMINATION", "AUTO_RENEWAL_PERPETUAL_TERM"]),
    ("Control & status", ["CHANGE_OF_CONTROL", "CURRENT_STATUS"]),
]

_NOT_ADDRESSED_TEXT = "Not addressed in any of the linked documents."

# Total characters of document text one group's extraction call will
# read, across every linked document combined. Generous — this is a
# handful of Cortex calls per contract, not a per-question cost — but
# still needs a ceiling for very large families. When the combined text
# would exceed it, the OLDEST documents are truncated/dropped first and
# the NEWEST kept whole, matching this project's established
# recency-wins precedence (see query_engine.py's contract_id parameter).
# UNVERIFIED past this value: the underlying model's actual context
# window on this account — this is a conservative guess, not a confirmed
# limit; lower it if a live run hits a "prompt too long" style error.
_FULL_TEXT_BUDGET_CHARS = 200000

# A single group's response holds a detailed paragraph-length value plus
# a verbatim quote for each of that group's ~5-8 fields — sized like
# index_builder.py's DETAILED-granularity budget for the same reason
# (complete_json retries with more room on its own if this isn't enough,
# up to utils/cortex_client.py's MAX_JSON_RETRY_TOKENS).
_GROUP_EXTRACTION_MAX_TOKENS = 8000

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


def _confidence_for_full_text(value: str, quote: str, source_doc_id: Optional[int]) -> str:
    """Same honest-heuristic spirit as this module's earlier retrieval-based
    version: HIGH when grounded in a verified verbatim quote, NOT_FOUND
    when the model explicitly said the documents don't address it.

    Between those two: every field still carries a citation — SOURCE_DOC_ID
    always names which document a value came from unless it's genuinely
    NOT_FOUND (get_contract_fields joins RAW_DOCUMENTS on it regardless of
    confidence, and the citation panel opens on it the same way for every
    confidence level) — but a value synthesized across several documents
    (e.g. "originally $X [Document 1], increased to $Y [Document 3]",
    exactly what this architecture is designed to produce) often has no
    SINGLE verbatim sentence that captures the whole synthesized answer,
    so _extract_field_group's quote check can legitimately come back empty
    even when the value is well-grounded. Collapsing that into the same
    LOW bucket as a value with no named source at all was misleading —
    MEDIUM distinguishes "has a citation, just not word-for-word verified"
    from LOW's "the model didn't even name a source for this," which is
    the genuinely rare, worth-a-look case."""
    lowered = (value or "").strip().lower()
    if not lowered or _NOT_ADDRESSED_TEXT.lower() in lowered:
        return "NOT_FOUND"
    if quote:
        return "HIGH"
    if source_doc_id:
        return "MEDIUM"
    return "LOW"


def _get_family_documents_for_extraction(session, project: ProjectConfig, contract_id: int) -> List[dict]:
    """Every document linked to this contract, oldest first, with its full
    RAW_TEXT — the input to the full-text extraction below. Same ordering
    as contract_linking.list_family_documents (EFFECTIVE_DATE, then
    SEQUENCE_NO, both NULLS LAST) so "oldest first" holds even when no
    EFFECTIVE_DATE is set (the common case today) — see that function and
    query_engine._recency_label for why SEQUENCE_NO is the fallback signal."""
    schema = project.qualified_schema
    rows = session.sql(
        f"""SELECT RD.DOC_ID, RD.FILE_NAME, RD.RAW_TEXT, CDL.DOC_ROLE,
                   CDL.EFFECTIVE_DATE, CDL.SEQUENCE_NO
            FROM {schema}.CONTRACT_DOCUMENT_LINK CDL
            JOIN {schema}.RAW_DOCUMENTS RD ON CDL.DOC_ID = RD.DOC_ID
            WHERE CDL.CONTRACT_ID = ?
            ORDER BY CDL.EFFECTIVE_DATE NULLS LAST, CDL.SEQUENCE_NO NULLS LAST, RD.FILE_NAME""",
        params=[contract_id],
    ).collect()
    return [dict(r.as_dict()) for r in rows]


_FULL_TEXT_EXTRACTION_PROMPT = """You are reviewing a contract and its full
family of amending documents for a contracts manager. Below are ALL the
documents linked to this contract, in chronological order (oldest first).
A later document can amend, extend, replace, or restate something an
earlier one said — when that happens, describe the evolution explicitly
(e.g. "originally set at $X [Document 1], increased to $Y under the
December 2025 Amendment Deed [Document 3]") rather than only stating the
final value alone. Extract SPECIFIC facts — exact dollar figures, dates,
clause numbers, defined terms, party names — verbatim as written, not
paraphrased or rounded.

Answer each of the following questions using ONLY what is actually in the
documents below. Give the most complete answer the documents actually
support: state what IS there, citing the document number, then note
specifically what's missing if anything is genuinely absent. Only answer
that a question isn't addressed when truly nothing relevant appears
anywhere in the ENTIRE set below — never merely because one document
alone doesn't fully resolve it while another does.

Where a question asks about a risk, obligation, restriction, or
compliance exposure (not a plain fact like a name, date, or dollar
figure), end the value with one short sentence assessing the practical
risk or exposure this creates FOR THE CLIENT who engaged the supplier/
counterparty named in the SUPPLIER field — i.e. the party this contract
register is maintained for, reviewing this as its own contracts manager
would — never the supplier's own risk. For example, a clause restricting
the SUPPLIER from disclosing information is a low risk to the client (it
protects the client), not a risk "to the supplier" for being restricted.
Use this style: "Assessment: consent required; medium risk for ownership
or entity changes." Commit to exactly one of Low, Medium, or High — never
hedge between two levels (e.g. "moderate-to-high"). Skip this sentence
for purely factual fields where there is no risk judgement to make.

QUESTIONS:
{questions_block}

DOCUMENTS:
{documents_block}

Return ONLY JSON in this shape, one entry per question key exactly as
given above:
{{
  "FIELD_KEY": {{"value": "...", "source_document": 2, "quote": "verbatim supporting excerpt from that document"}}
}}
"value" should be a thorough paragraph reflecting everything relevant
found across the documents, exactly as you would brief a contracts
manager — not a one-line fragment. "source_document" is the number of the
SINGLE document (from the list above) that most directly supports the
final/current answer (the most recent one that addresses it, if it
evolved across documents). "quote" must be copied VERBATIM, character for
character, from that specific document — leave it as an empty string
rather than paraphrasing something and calling it a quote if you cannot
find an exact supporting passage. If a question is genuinely not
addressed anywhere, still include its key with exactly
{{"value": "{not_addressed}", "source_document": null, "quote": ""}}.
"""


def _extract_field_group(session, project: ProjectConfig, group_label: str, field_keys: list,
                         ordered_docs: List[dict]) -> Dict[str, dict]:
    """Runs one "agent" — one Cortex call reading every linked document in
    full — for one group of related fields. Returns
    {field_key: {"value", "source_doc_id", "quote"}}."""
    questions_block = "\n".join(f"- {key}: {_QUESTIONS[key]}" for key in field_keys)

    # Budget: keep the NEWEST documents whole, truncate/drop the OLDEST
    # ones first if the combined text would be too large for one call.
    budget = _FULL_TEXT_BUDGET_CHARS
    text_by_doc_id: Dict[int, str] = {}
    for doc in reversed(ordered_docs):  # newest first for budget allocation
        if budget <= 0:
            break
        text = (doc["RAW_TEXT"] or "")[:budget]
        text_by_doc_id[doc["DOC_ID"]] = text
        budget -= len(text)

    doc_number_by_id: Dict[int, int] = {}
    documents_block_parts = []
    for doc in ordered_docs:  # display order stays oldest-first regardless of budget order
        if doc["DOC_ID"] not in text_by_doc_id:
            continue
        n = len(doc_number_by_id) + 1
        doc_number_by_id[doc["DOC_ID"]] = n
        role_desc = (doc.get("DOC_ROLE") or "").replace("_", " ").title() or "Document"
        date_bit = f", effective {doc['EFFECTIVE_DATE']}" if doc.get("EFFECTIVE_DATE") else ""
        documents_block_parts.append(
            f"[Document {n}] {doc['FILE_NAME']} ({role_desc}{date_bit})\n{text_by_doc_id[doc['DOC_ID']]}"
        )

    prompt = _FULL_TEXT_EXTRACTION_PROMPT.format(
        questions_block=questions_block,
        documents_block="\n\n".join(documents_block_parts),
        not_addressed=_NOT_ADDRESSED_TEXT,
    )
    result = complete_json(session, project.active_model, prompt, max_tokens=_GROUP_EXTRACTION_MAX_TOKENS)

    doc_id_by_number = {n: doc_id for doc_id, n in doc_number_by_id.items()}

    parsed: Dict[str, dict] = {}
    for key in field_keys:
        entry = result.get(key) or {}
        value = (entry.get("value") or "").strip()
        source_doc_id = doc_id_by_number.get(entry.get("source_document"))
        quote = (entry.get("quote") or "").strip()
        if quote:
            normalized_quote = _normalize_for_match(quote)
            claimed_text = text_by_doc_id.get(source_doc_id, "") if source_doc_id else ""
            if normalized_quote not in _normalize_for_match(claimed_text):
                # The model can misattribute which of several documents a
                # genuine verbatim quote came from when reading them all in
                # one call — before discarding an otherwise-real quote as
                # unverified (see _extract_highlight_phrase's identical
                # principle), check the rest of the family too and correct
                # the attribution rather than losing a real quote (and the
                # HIGH confidence it earns) to a wrong document number.
                matched_doc_id = next(
                    (doc_id for doc_id, text in text_by_doc_id.items()
                     if doc_id != source_doc_id and normalized_quote in _normalize_for_match(text)),
                    None,
                )
                if matched_doc_id:
                    source_doc_id = matched_doc_id
                else:
                    quote = ""
        parsed[key] = {"value": value, "source_doc_id": source_doc_id, "quote": quote}
    return parsed


def extract_stock_fields_for_contract(session, project: ProjectConfig, contract_id: int,
                                      on_progress=None) -> List[FieldExtractResult]:
    """Runs every stock question for one contract and upserts
    CONTRACT_FIELD_EXTRACTS — this is what populates "the History" the
    Contract Lookup page reads from. Safe to re-run any time — e.g. after
    a new variation/extension is linked into the family, or a linked
    document's content changed (see is_extraction_current) — since each
    field is a MERGE keyed on (CONTRACT_ID, FIELD_KEY), not an append. A
    field a reviewer already marked IS_VERIFIED is overwritten like any
    other on re-run: re-verifying after a contract's documents change is a
    deliberate design choice, not an oversight — an unreviewed
    re-extraction should not silently keep an old field flagged as
    verified once its source documents have changed.

    CONFIRMED on a live account: the original design here (one
    query_engine.search() call per field, scoped to the family via
    restrict_to_doc_ids) kept returning "the excerpts provided do not
    contain sufficient information..." for most fields on real, densely-
    written contracts — the retrieval/reranking pipeline that design
    depends on wasn't reliably surfacing the right section for a given
    question even when the answer was genuinely in the documents. Rather
    than tune that pipeline further, this now reads every linked
    document's FULL text directly, grouped into one Cortex call per
    template section (FIELD_GROUPS — "agents", each looping over every
    document in the family) — closing the retrieval gap entirely for a
    workload where the total text (a handful of PDFs) comfortably fits in
    one call. See _extract_field_group for the budget/truncation rule
    when a family's combined text is unusually large.

    on_progress, when given, is called once per group ("agent") — see
    ingestion/stage_pickup.py's identical convention.

    Also (re)generates the contract's Executive Assessment narrative,
    Recommended Actions, classification scorecard, Key Commercial Risks /
    Procurement Recommendation / Retender Strategy, and the condensed
    one-sentence-per-field summary once every field has been extracted —
    see generate_contract_overview, generate_recommended_actions,
    generate_classification_scorecard, generate_procurement_strategy,
    generate_condensed_summary.
    """
    schema = project.qualified_schema
    ordered_docs = _get_family_documents_for_extraction(session, project, contract_id)

    results: List[FieldExtractResult] = []
    for i, (group_label, field_keys) in enumerate(FIELD_GROUPS, start=1):
        if on_progress:
            on_progress(f"Agent {i}/{len(FIELD_GROUPS)} ({group_label}): "
                        f"reading {len(ordered_docs)} document(s)…")
        parsed = _extract_field_group(session, project, group_label, field_keys, ordered_docs)

        for field_key in field_keys:
            entry = parsed[field_key]
            value, source_doc_id, quote = entry["value"], entry["source_doc_id"], entry["quote"]
            confidence = _confidence_for_full_text(value, quote, source_doc_id)
            highlight_phrase = _extract_highlight_phrase(session, project, value, quote) if quote else None

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
                params=[contract_id, field_key, value[:4000],
                        source_doc_id, None, quote[:4000] if quote else None,
                        highlight_phrase, confidence, project.active_model],
            ).collect()

            results.append(FieldExtractResult(field_key, confidence))
        if on_progress:
            on_progress(f"Agent {i}/{len(FIELD_GROUPS)} ({group_label}): done")

    log_event(logger, "CONTRACT_EXTRACTED", project.project_code,
              contract_id=contract_id,
              not_found=sum(1 for r in results if r.confidence == "NOT_FOUND"))

    generate_contract_overview(session, project, contract_id)
    generate_recommended_actions(session, project, contract_id)
    generate_classification_scorecard(session, project, contract_id)
    generate_procurement_strategy(session, project, contract_id)
    generate_condensed_summary(session, project, contract_id)
    return results


def extract_stock_fields_for_all_contracts(session, project: ProjectConfig, on_progress=None) -> dict:
    """Runs extract_stock_fields_for_contract for every contract in the
    register — the "Run extraction for all contracts" button, and (at
    Scale-phase volume) what a scheduled Task would call instead of a
    Streamlit click. Returns {contract_id: [FieldExtractResult, ...]}.

    on_progress, when given, is called once per contract plus once per
    agent within that contract (prefixed with the contract's CW number),
    matching the single-contract button's live per-agent status line
    instead of one opaque spinner for the whole multi-contract run."""
    families = contract_linking.list_contract_families(session, project)
    results = {}
    for i, family in enumerate(families, start=1):
        if on_progress:
            on_progress(f"Contract {i}/{len(families)}: {family.cw_number}…")
        results[family.contract_id] = extract_stock_fields_for_contract(
            session, project, family.contract_id,
            on_progress=(lambda msg, cw=family.cw_number: on_progress(f"{cw}: {msg}")) if on_progress else None,
        )
    return results


def _answered_fields_text(session, project: ProjectConfig, contract_id: int) -> Optional[str]:
    fields = get_contract_fields(session, project, contract_id)
    answered = [f for f in fields if f.get("FIELD_VALUE")]
    if not answered:
        return None
    return "\n\n".join(
        f"{FIELD_LABELS.get(f['FIELD_KEY'], f['FIELD_KEY'])}: {f['FIELD_VALUE']}" for f in answered
    )


_OVERVIEW_PROMPT = """Write a short executive assessment of this contract
for a contracts manager who has never seen it, in EXACTLY 4-6 sentences
and NO MORE THAN 120 WORDS TOTAL — this is a top-of-page elevator
summary, not a briefing. Cover: what it is, the parties if named, its
scope, its term, and only the ONE or TWO most important things requiring
attention. Do not itemize a list of issues, even inside prose (no
"First,... Second,... Third,..." or similar enumeration) — the detailed
findings already live in the tables below this summary; this is the
short version that sits above them. Base it only on the information
given below. Write it as plain prose, in a professional, direct tone.

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

    # CONFIRMED on a live account: the prior "4-6 sentences" instruction
    # alone wasn't followed reliably — the model produced a numbered,
    # multi-paragraph list of issues well past 6 sentences instead, and
    # the generous max_tokens=600 ceiling didn't push back on that. The
    # tighter word cap above plus a much lower ceiling here forces actual
    # brevity rather than just asking nicely for it.
    overview = complete(session, project.active_model,
                        _OVERVIEW_PROMPT.format(fields_text=fields_text), max_tokens=300)

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
contradicts it. Be decisive, not hedged: if the assessment describes
safety-critical, 24/7, or network-wide operations, a high contract value,
or substantial liquidated-damages/KPI exposure, overall_classification
and operational_exposure_rating should say so as High — do not default to
a middle "Moderate" rating out of caution when the underlying findings
clearly support a higher one.

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


_PROCUREMENT_STRATEGY_PROMPT = """Based on the extracted contract assessment
below, provide a forward-looking procurement wrap-up for a contracts
manager, in the same spirit as a reviewer's closing recommendation:

- key_commercial_risks: a list of the 3-6 most significant commercial
  risks this contract poses to the client (the party that engaged the
  supplier — never the supplier's own risk), each one short sentence,
  ranked most severe first. Only include risks actually supported by the
  assessment below — do not invent generic risks that aren't grounded in
  it.
- procurement_recommendation: 1-2 sentences recommending an overall course
  of action for this contract (e.g. continue and monitor; renegotiate at
  the next extension; begin retender planning), grounded in what the
  assessment actually shows — not generic boilerplate.
- retender_strategy: 2-4 sentences on how the client should approach its
  next decision point for this contract — extend, renegotiate, or
  retender — covering what leverage or constraints the client has (e.g.
  notice periods already given, EA/labour cost exposure, KPI performance
  to date) and what should be reassessed (pricing, security, KPIs, scope)
  if a new deed or contract is negotiated.

EXTRACTED ASSESSMENT:
{fields_text}

Return ONLY JSON: {{"key_commercial_risks": ["...", "..."],
"procurement_recommendation": "...", "retender_strategy": "..."}}
"""


def generate_procurement_strategy(session, project: ProjectConfig, contract_id: int) -> Dict[str, object]:
    """Synthesizes CONTRACT_REGISTER.PROCUREMENT_STRATEGY — the template's
    "Key Commercial Risks" / "Procurement Recommendation" / "Retender
    Strategy" sections, rendered after the Consolidated Procurement
    Assessment scorecard — from this contract's already-extracted stock
    fields, matching the CoPilot reference summary CW20841 was
    benchmarked against, which closes with exactly this kind of
    forward-looking strategic wrap-up (CONFIRMED missing from this app's
    own output in that comparison). Returns {} (and clears
    PROCUREMENT_STRATEGY) if no fields have been extracted yet."""
    schema = project.qualified_schema
    fields_text = _answered_fields_text(session, project, contract_id)
    if not fields_text:
        session.sql(
            f"UPDATE {schema}.CONTRACT_REGISTER SET PROCUREMENT_STRATEGY = NULL WHERE CONTRACT_ID = ?",
            params=[contract_id],
        ).collect()
        return {}

    try:
        result = complete_json(session, project.active_model,
                               _PROCUREMENT_STRATEGY_PROMPT.format(fields_text=fields_text), max_tokens=1200)
        strategy = {
            "KEY_COMMERCIAL_RISKS": [r.strip() for r in result.get("key_commercial_risks", []) if r and r.strip()],
            "PROCUREMENT_RECOMMENDATION": (result.get("procurement_recommendation") or "").strip(),
            "RETENDER_STRATEGY": (result.get("retender_strategy") or "").strip(),
        }
    except Exception:  # noqa: BLE001 — a synthesis step failing shouldn't fail the whole extraction run
        logger.warning("EVENT=PROCUREMENT_STRATEGY_FAILED contract_id=%s", contract_id, exc_info=True)
        strategy = {}

    import json
    session.sql(
        f"""UPDATE {schema}.CONTRACT_REGISTER
            SET PROCUREMENT_STRATEGY = PARSE_JSON(?)
            WHERE CONTRACT_ID = ?""",
        params=[json.dumps(strategy), contract_id],
    ).collect()
    return strategy


_CONDENSED_FIELDS_PROMPT = """Below are detailed findings already
extracted for a contract, one per field. Compress EACH into ONE short
sentence (two at most) for a one-to-two-page executive summary: keep the
single most important fact — a date, dollar figure, party name, or an
explicit Low/Medium/High risk call already stated in the finding — and
drop supporting clause-by-clause detail, quotations, and hedging
language. If a finding is already one short sentence, return it
essentially unchanged.

FIELDS:
{fields_block}

Return ONLY JSON: {{"FIELD_KEY": "condensed sentence", ...}} — one entry
for every field key listed above.
"""


def generate_condensed_summary(session, project: ProjectConfig, contract_id: int) -> Dict[str, str]:
    """Synthesizes CONTRACT_REGISTER.CONDENSED_FIELDS — a one-to-two-
    sentence version of every already-extracted stock field, used only by
    the 2-page condensed Word/PDF output (docx_report.build_contract_docx_condensed
    / pdf_report.build_contract_pdf_condensed) so that report can fit in
    roughly the same length as the CoPilot-generated reference summary
    CW20841 was benchmarked against — the full-length report keeps every
    field's full paragraph-length FIELD_VALUE untouched. This condenses
    already-synthesized text, not raw documents, so it's a single cheap
    call regardless of how large the contract's document family is.
    Returns {} (and clears CONDENSED_FIELDS) if no fields have been
    extracted yet."""
    schema = project.qualified_schema
    fields = get_contract_fields(session, project, contract_id)
    answered = [f for f in fields if f.get("FIELD_VALUE")]
    if not answered:
        session.sql(
            f"UPDATE {schema}.CONTRACT_REGISTER SET CONDENSED_FIELDS = NULL WHERE CONTRACT_ID = ?",
            params=[contract_id],
        ).collect()
        return {}

    fields_block = "\n\n".join(
        f"- {f['FIELD_KEY']} ({FIELD_LABELS.get(f['FIELD_KEY'], f['FIELD_KEY'])}): {f['FIELD_VALUE']}"
        for f in answered
    )
    try:
        result = complete_json(session, project.active_model,
                               _CONDENSED_FIELDS_PROMPT.format(fields_block=fields_block), max_tokens=4000)
        condensed = {
            key: value.strip()
            for key, value in result.items()
            if key in _QUESTIONS and value and value.strip()
        }
    except Exception:  # noqa: BLE001 — a synthesis step failing shouldn't fail the whole extraction run
        logger.warning("EVENT=CONDENSED_SUMMARY_FAILED contract_id=%s", contract_id, exc_info=True)
        condensed = {}

    import json
    session.sql(
        f"""UPDATE {schema}.CONTRACT_REGISTER
            SET CONDENSED_FIELDS = PARSE_JSON(?)
            WHERE CONTRACT_ID = ?""",
        params=[json.dumps(condensed), contract_id],
    ).collect()
    return condensed


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


def build_fields_table(session, project: ProjectConfig, contract_id: int) -> List[dict]:
    """Flattens get_contract_fields() into one row per stock field, shaped
    for a spreadsheet-style review table — the Contract Register/Contract
    Lookup pages' tabular view (Section, Value, Source, Verified), as an
    alternative to the per-field boxed layout. Each row carries every raw
    field straight through (SOURCE_STAGE_PATH, SOURCE_QUOTE, etc.) so the
    calling page can build a citation link (citation_viewer.py) or render
    a checkbox without a second query. Only ONE source is ever recorded
    per field today (CONTRACT_FIELD_EXTRACTS.SOURCE_DOC_ID is a single
    column, not a list — see extract_stock_fields_for_contract's
    _extract_field_group, which picks the single document the model
    named as most directly supporting the answer, even though a field's
    value may describe how the answer evolved across several documents)
    — so a row's single "Source" link points at that one document, not a
    link per document the value text narrates."""
    fields = get_contract_fields(session, project, contract_id)
    return [
        {
            "field_key": f["FIELD_KEY"],
            "section": SECTION_FOR_FIELD.get(f["FIELD_KEY"], ""),
            "label": FIELD_LABELS.get(f["FIELD_KEY"], f["FIELD_KEY"]),
            "value": f.get("FIELD_VALUE"),
            "confidence": f.get("CONFIDENCE"),
            "source_quote": f.get("SOURCE_QUOTE"),
            "source_file_name": f.get("SOURCE_FILE_NAME"),
            "source_stage_path": f.get("SOURCE_STAGE_PATH"),
            "is_verified": bool(f.get("IS_VERIFIED")),
        }
        for f in fields
    ]


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
