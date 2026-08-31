"""
contract_extraction.py — automatically answer the 15 stock questions for
every contract and persist the results with citations.

LEX-specific: not part of the generic project-llm-wiki template this was
forked from. Rather than a bespoke single-shot "dump the whole contract
and ask 15 questions at once" prompt, each stock question is answered by
calling query_engine.search() — the exact same function chat uses — scoped
to one contract's linked documents via restrict_to_doc_ids. This means
extraction shares chat's retrieval, citation, and "say so rather than
guessing" behaviour exactly, with no second engine to keep in sync, and it
naturally covers a contract's variations/extensions since those are all
linked documents in the same family.
"""

from dataclasses import dataclass
from typing import List

from config import ProjectConfig
import contract_linking
from query_engine import search
from utils.logging_utils import get_logger, log_event

logger = get_logger(__name__)

# The 15 stock questions/entities every contract gets answered for
# automatically. FIELD_KEY is what's stored in CONTRACT_FIELD_EXTRACTS;
# QUESTION is fed to query_engine.search() exactly as a user's own
# question would be. Order here is the order they're extracted in and the
# order the Contract Register page displays them in.
STOCK_FIELDS = [
    ("CONTRACT_TITLE", "What is the contract title?"),
    ("CW_NUMBER", "What is the CW number (contract/work order number) for this contract?"),
    ("CONTRACT_END_DATE", "What is the contract end date (expiry date)?"),
    ("CONTRACT_SUMMARY", "In one sentence, summarise what this contract is for."),
    ("NOVATION_CONSENT",
     "Does the novation clause require consent, and if so from whom? Quote the relevant clause."),
    ("DISCLOSURE_CLAUSE", "What does the disclosure clause require or restrict?"),
    ("EXTENSION_OPTIONS",
     "What extension options exist under this contract — how many, for how long, and under what conditions?"),
    ("COMPLEXITY_GOODS_SERVICES",
     "Describe the complexity of the goods and/or services being supplied under this contract."),
    ("SEPARABLE_PORTIONS",
     "Does this contract identify separable portions of the work, and if so what are they?"),
    ("PAYMENT_REGIME",
     "What is the payment regime under this contract — is it a claims process, milestone payments, or "
     "something else, and how does it work?"),
    ("SECURITIES",
     "What securities (e.g. bank guarantees, cash retention) are required under this contract, and in "
     "what amounts?"),
    ("PRICE_REVIEW_MECHANISM", "What price review or price escalation mechanisms does this contract include?"),
    ("EA_CLAUSES", "What Enterprise Agreement (EA) related clauses, if any, does this contract include?"),
    ("TERMINATION_CLAUSE",
     "What are the termination clauses in this contract, including notice periods and any restrictions "
     "such as perpetual-contract provisions?"),
    ("AUTO_RENEWAL_MECHANISM",
     "Does this contract auto-renew? If so, describe the mechanism, and state whether a change of "
     "ownership or the auto-renewal cycle triggers a right to renegotiate the contract's terms and "
     "conditions."),
]

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


def extract_stock_fields_for_contract(session, project: ProjectConfig, contract_id: int) -> List[FieldExtractResult]:
    """Runs all 15 stock questions for one contract, scoped to its linked
    documents, and upserts CONTRACT_FIELD_EXTRACTS. Safe to re-run any
    time — e.g. after a new variation/extension is linked into the family
    — since each field is a MERGE keyed on (CONTRACT_ID, FIELD_KEY), not an
    append. A field a reviewer already marked IS_VERIFIED is overwritten
    like any other on re-run: re-verifying after a contract's documents
    change is a deliberate design choice, not an oversight — an unreviewed
    re-extraction should not silently keep an old field flagged as
    verified once its source documents have changed."""
    schema = project.qualified_schema
    family_doc_ids = contract_linking.get_family_doc_ids(session, project, contract_id)

    results: List[FieldExtractResult] = []
    for field_key, question in STOCK_FIELDS:
        answer = search(session, project, question, use_cache=True,
                        restrict_to_doc_ids=family_doc_ids)
        confidence = _confidence_for(answer.answer, answer.cited_docs)
        top = answer.top_citation

        session.sql(
            f"""MERGE INTO {schema}.CONTRACT_FIELD_EXTRACTS AS tgt
                USING (SELECT ? AS CONTRACT_ID, ? AS FIELD_KEY, ? AS FIELD_VALUE,
                              ? AS SOURCE_DOC_ID, ? AS SOURCE_NODE_ID,
                              ? AS CONFIDENCE, ? AS MODEL_USED) AS src
                ON tgt.CONTRACT_ID = src.CONTRACT_ID AND tgt.FIELD_KEY = src.FIELD_KEY
                WHEN MATCHED THEN UPDATE SET
                    FIELD_VALUE = src.FIELD_VALUE, SOURCE_DOC_ID = src.SOURCE_DOC_ID,
                    SOURCE_NODE_ID = src.SOURCE_NODE_ID, CONFIDENCE = src.CONFIDENCE,
                    MODEL_USED = src.MODEL_USED, EXTRACTED_AT = CURRENT_TIMESTAMP(),
                    IS_VERIFIED = FALSE, VERIFIED_BY = NULL, VERIFIED_AT = NULL
                WHEN NOT MATCHED THEN INSERT
                    (CONTRACT_ID, FIELD_KEY, FIELD_VALUE, SOURCE_DOC_ID, SOURCE_NODE_ID,
                     CONFIDENCE, MODEL_USED)
                    VALUES (src.CONTRACT_ID, src.FIELD_KEY, src.FIELD_VALUE, src.SOURCE_DOC_ID,
                            src.SOURCE_NODE_ID, src.CONFIDENCE, src.MODEL_USED)""",
            params=[contract_id, field_key, answer.answer[:4000],
                    top["doc_id"] if top else None, top["node_id"] if top else None,
                    confidence, project.active_model],
        ).collect()

        results.append(FieldExtractResult(field_key, confidence))

    log_event(logger, "CONTRACT_EXTRACTED", project.project_code,
              contract_id=contract_id,
              not_found=sum(1 for r in results if r.confidence == "NOT_FOUND"))
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


def get_contract_fields(session, project: ProjectConfig, contract_id: int) -> List[dict]:
    """All extracted fields for one contract, with the source document's
    file name/URL joined in for display — the Contract Register page's
    per-contract expander reads this directly."""
    schema = project.qualified_schema
    rows = session.sql(
        f"""SELECT CFE.FIELD_KEY, CFE.FIELD_VALUE, CFE.CONFIDENCE, CFE.SOURCE_QUOTE,
                   CFE.IS_VERIFIED, CFE.VERIFIED_BY, CFE.VERIFIED_AT, CFE.EXTRACTED_AT,
                   RD.FILE_NAME AS SOURCE_FILE_NAME, RD.SOURCE_URL AS SOURCE_URL
            FROM {schema}.CONTRACT_FIELD_EXTRACTS CFE
            LEFT JOIN {schema}.RAW_DOCUMENTS RD ON CFE.SOURCE_DOC_ID = RD.DOC_ID
            WHERE CFE.CONTRACT_ID = ?""",
        params=[contract_id],
    ).collect()
    by_key = {r["FIELD_KEY"]: dict(r.as_dict()) for r in rows}
    # Always return all 15 in STOCK_FIELDS order, even if extraction hasn't
    # run yet for this contract — the UI shows "not yet extracted" rather
    # than silently omitting a field.
    return [by_key.get(key, {"FIELD_KEY": key, "FIELD_VALUE": None, "CONFIDENCE": None})
            for key, _ in STOCK_FIELDS]


def set_field_verified(session, project: ProjectConfig, contract_id: int, field_key: str,
                       is_verified: bool, verified_by: str) -> None:
    schema = project.qualified_schema
    session.sql(
        f"""UPDATE {schema}.CONTRACT_FIELD_EXTRACTS
            SET IS_VERIFIED = ?, VERIFIED_BY = ?, VERIFIED_AT = CURRENT_TIMESTAMP()
            WHERE CONTRACT_ID = ? AND FIELD_KEY = ?""",
        params=[is_verified, verified_by if is_verified else None, contract_id, field_key],
    ).collect()
