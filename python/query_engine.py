"""
query_engine.py — tree-search + answer synthesis, generalized to run against
any project's schema.

Forked from the project-llm-wiki template (ds-madhavan-ramani/org_mm_chat).
Difference from that template: search() takes an optional
restrict_to_doc_ids parameter. When given, Stage 1's document-summary
routing (and its keyword-fallback) is skipped entirely — the caller has
already decided which documents matter — and Stage 2 section retrieval
runs scoped to exactly those documents. This is what lets LEX's stock-field
extraction (contract_extraction.py) reuse this exact function, scoped to
one contract's linked documents, instead of needing a second retrieval
engine: extraction is just chat with the document set pre-selected.
"""

import hashlib
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from config import ProjectConfig, DATABASE, CATALOG_SCHEMA
from ingestion.index_builder import EMBED_MODEL
from utils.cortex_client import complete, complete_json, CortexError
from utils.logging_utils import get_logger, log_event

logger = get_logger(__name__)

# How much evidence a single question can pull from. Higher = more complete
# answers for broad/thematic questions spanning many sections, at the cost
# of a longer, slower, more expensive synthesis call per question. Tune
# here rather than as magic numbers buried in the routing prompts below.
# The document-routing cap is per-project (PROJECTS.MAX_CANDIDATE_DOCS,
# clamped to [5, 10] by ProjectConfig.clamped_max_candidate_docs) — this
# constant is only the fallback used by prompt text before that value is
# known within search().
MAX_CANDIDATE_DOCS = 10
MAX_CANDIDATE_SECTIONS = 20
MAX_KEYWORD_FALLBACK_DOCS = 10
MAX_VECTOR_CANDIDATES = 15
# EMBED_MODEL imported from index_builder.py, not redefined here — the
# question and every indexed section need to be embedded with the same
# model to be comparable via VECTOR_COSINE_SIMILARITY, so this can't drift
# out of sync with what indexing actually used.


@dataclass
class AnswerResult:
    answer: str
    # [{"number": 1, "file_name": ..., "url": ...}, ...] — url is None for
    # directly-uploaded documents (no SharePoint source to link to). Older
    # cached rows (from before citations were structured) may still hold
    # plain filename strings; render defensively for both shapes.
    cited_docs: List[Dict] = field(default_factory=list)
    nodes_visited: List[int] = field(default_factory=list)
    from_cache: bool = False

    @property
    def top_citation(self) -> Optional[Dict]:
        """First (lowest-numbered) citation, or None if the answer wasn't
        grounded in any document — used by contract_extraction.py to pick
        one SOURCE_DOC_ID/SOURCE_NODE_ID to record per stock field."""
        return self.cited_docs[0] if self.cited_docs else None


def _normalize_answer_text(text: str) -> str:
    """Some models emit literal backslash-n / backslash-t escape sequences
    in plain-text (non-JSON) responses instead of real whitespace — the
    same unreliable-formatting quirk complete_json() works around for
    structured responses, just showing up here as visible '\\n' text in
    the chat UI instead of a JSON parse error."""
    return (
        text.replace("\\r\\n", "\n")
            .replace("\\n", "\n")
            .replace("\\t", "\t")
    )


def search(session, project: ProjectConfig, question: str, use_cache: bool = True,
           restrict_to_doc_ids: Optional[List[int]] = None) -> AnswerResult:
    """
    restrict_to_doc_ids: when given, skips Stage 1's document-summary
    routing entirely and searches only within these documents' sections —
    used by contract_extraction.py to scope a stock question to one
    contract's linked documents (base + variations/extensions/novations).
    An empty list (a contract with no linked documents yet) short-circuits
    to a clear answer rather than falling through to project-wide routing.
    None (the default) is ordinary free-form chat: every document in the
    project is a routing candidate, exactly as before this parameter existed.
    """
    _validate_question(question)
    schema = project.qualified_schema

    if restrict_to_doc_ids is not None and not restrict_to_doc_ids:
        return AnswerResult(answer="No documents are linked to this contract yet.")

    doc_count = session.sql(
        f"SELECT COUNT(*) AS C FROM {schema}.RAW_DOCUMENTS"
    ).collect()[0]["C"]
    if doc_count == 0:
        return AnswerResult(
            answer=("No documents have been added to this project yet. "
                    "Go to the Data Sources page to upload files or connect "
                    "a SharePoint folder, then come back and ask again."),
        )

    # Scoped queries (restrict_to_doc_ids) need the doc scope baked into
    # the cache key — otherwise two different contracts asking the same
    # canned stock question (e.g. "What is the contract end date?") would
    # collide on the same QUERY_HASH and one contract's cached answer
    # would incorrectly be served for another's. Ordinary chat questions
    # (restrict_to_doc_ids=None) hash exactly as before.
    scope_key = ",".join(str(d) for d in sorted(restrict_to_doc_ids)) if restrict_to_doc_ids else ""
    query_hash = hashlib.md5(f"{question.strip().lower()}|{scope_key}".encode()).hexdigest()
    if use_cache:
        cached = _check_cache(session, project, query_hash)
        if cached:
            return cached

    start = time.time()

    if restrict_to_doc_ids is not None:
        candidate_doc_ids = list(restrict_to_doc_ids)
    else:
        # Stage 1: pick relevant document(s) from top-level summaries
        doc_nodes = session.sql(
            f"""SELECT DI.NODE_ID, DI.DOC_ID, DI.NODE_TITLE, DI.NODE_SUMMARY
                FROM {schema}.DOCUMENT_INDEX DI
                WHERE DI.PARENT_NODE_ID IS NULL"""
        ).collect()

        if not doc_nodes:
            return AnswerResult(
                answer=("Documents have been added but haven't been indexed yet. "
                        "Trigger a rebuild from the Data Sources page."),
            )

        max_candidate_docs = project.clamped_max_candidate_docs
        doc_summary_text = "\n".join(
            f"[doc_id={d['DOC_ID']}] {d['NODE_TITLE']}: {d['NODE_SUMMARY']}" for d in doc_nodes
        )
        routing_prompt = f"""Given this question and list of documents, return the
doc_id values (as a JSON list of integers) of documents likely to contain the
answer. Return at most {max_candidate_docs}. If none look relevant, return an
empty list.

QUESTION: {question}

DOCUMENTS:
{doc_summary_text}

Return ONLY JSON: {{"doc_ids": [1, 2]}}"""

        routing = complete_json(session, project.active_model, routing_prompt)
        candidate_doc_ids = routing.get("doc_ids", [])[:max_candidate_docs]

        if not candidate_doc_ids:
            # Document-level summaries are broad glosses of a whole
            # document — a specific clause number/defined term will rarely
            # appear in one even when it's right there in the raw text.
            # Fall back to a literal keyword search instead of giving up
            # outright.
            candidate_doc_ids = _keyword_fallback_doc_ids(session, project, question, schema)
            if not candidate_doc_ids:
                return AnswerResult(answer="I couldn't find a document relevant to that question.")

    # Stage 2: within selected documents, pick relevant section(s)
    placeholders = ", ".join(["?"] * len(candidate_doc_ids))
    section_nodes = session.sql(
        f"""SELECT NODE_ID, DOC_ID, NODE_TITLE, NODE_SUMMARY, NODE_TEXT_REF
            FROM {schema}.DOCUMENT_INDEX
            WHERE DOC_ID IN ({placeholders}) AND NODE_LEVEL = 'section'""",
        params=candidate_doc_ids,
    ).collect()

    # Hybrid retrieval (optional, PROJECTS.ENABLE_VECTOR_SEARCH): union in
    # sections found by vector/semantic similarity to the question. When
    # restrict_to_doc_ids is set, this is scoped to just those documents
    # too (a stock question about one contract has no business pulling in
    # a semantically-similar section from an unrelated contract); for
    # ordinary chat it's project-wide, independent of which documents
    # Stage 1's summary-based routing selected, since a document summary
    # is a compression that can miss a semantically-relevant section
    # entirely even when the wording doesn't match well.
    if project.enable_vector_search:
        known_node_ids = {s["NODE_ID"] for s in section_nodes}
        vector_scope_doc_ids = candidate_doc_ids if restrict_to_doc_ids is not None else None
        vector_node_ids = [
            nid for nid in _vector_search_section_ids(session, question, schema, vector_scope_doc_ids)
            if nid not in known_node_ids
        ]
        if vector_node_ids:
            vec_placeholders = ", ".join(["?"] * len(vector_node_ids))
            section_nodes = section_nodes + session.sql(
                f"""SELECT NODE_ID, DOC_ID, NODE_TITLE, NODE_SUMMARY, NODE_TEXT_REF
                    FROM {schema}.DOCUMENT_INDEX WHERE NODE_ID IN ({vec_placeholders})""",
                params=vector_node_ids,
            ).collect()

    if project.enable_reranking:
        # LLM judges/filters the (possibly vector-widened) section pool
        # before synthesis — effectively a reranker. Optional
        # (PROJECTS.ENABLE_RERANKING): an extra Cortex call per question,
        # skippable when the section pool is already small/precise enough
        # that judging it isn't worth the added latency/cost.
        section_summary_text = "\n".join(
            f"[node_id={s['NODE_ID']}] {s['NODE_TITLE']}: {s['NODE_SUMMARY']}" for s in section_nodes
        )
        section_prompt = f"""Given this question and list of document sections,
return the node_id values (JSON list of integers) of sections likely to
contain the answer. Return at most {MAX_CANDIDATE_SECTIONS}.

QUESTION: {question}

SECTIONS:
{section_summary_text}

Return ONLY JSON: {{"node_ids": [1, 2]}}"""

        section_routing = complete_json(session, project.active_model, section_prompt)
        node_ids = section_routing.get("node_ids", [])[:MAX_CANDIDATE_SECTIONS]
    else:
        node_ids = []

    if not node_ids:
        # Section routing found nothing specific within otherwise-relevant
        # documents (or reranking is disabled) — rather than give up, fall
        # back to every section in those documents (still bounded by the
        # section cap) instead of depending on the routing model having
        # correctly picked among them; an over-conservative section pick
        # shouldn't produce an empty answer when the document itself was
        # judged relevant.
        node_ids = [s["NODE_ID"] for s in section_nodes][:MAX_CANDIDATE_SECTIONS]
        if not node_ids:
            return AnswerResult(answer="I found relevant documents but no specific section answers that question.")

    # Stage 3: pull raw text for selected sections and synthesize
    placeholders = ", ".join(["?"] * len(node_ids))
    selected = session.sql(
        f"""SELECT DI.NODE_ID, DI.NODE_TITLE, DI.NODE_TEXT_REF,
                   RD.DOC_ID, RD.FILE_NAME, RD.RAW_TEXT, RD.SOURCE_URL
            FROM {schema}.DOCUMENT_INDEX DI
            JOIN {schema}.RAW_DOCUMENTS RD ON DI.DOC_ID = RD.DOC_ID
            WHERE DI.NODE_ID IN ({placeholders})""",
        params=node_ids,
    ).collect()

    # Number sources deterministically in code (not left to the model) —
    # this is what actually appears in the Sources list and any [n]
    # markers the model uses are just following along with it, so the
    # citation list is always correct even if the model's inline markers
    # aren't.
    doc_urls = {}
    doc_id_by_name = {}
    for s in selected:
        doc_urls.setdefault(s["FILE_NAME"], s["SOURCE_URL"])
        doc_id_by_name.setdefault(s["FILE_NAME"], s["DOC_ID"])
    file_names_sorted = sorted(doc_urls)
    doc_numbers = {name: i + 1 for i, name in enumerate(file_names_sorted)}

    context_chunks = []
    node_id_by_name = {}
    for s in selected:
        start_off, end_off = (int(x) for x in s["NODE_TEXT_REF"].split(":"))
        excerpt = s["RAW_TEXT"][start_off:end_off][: project.max_section_chars]
        n = doc_numbers[s["FILE_NAME"]]
        context_chunks.append(f"[{n}] {s['FILE_NAME']} — {s['NODE_TITLE']}\n{excerpt}")
        node_id_by_name.setdefault(s["FILE_NAME"], s["NODE_ID"])

    synthesis_prompt = f"""Answer the question using ONLY the excerpts below.
Write in a concise, professional tone, as if briefing a contracts manager —
lead with the direct answer, then the supporting clause language, and stop;
do not restate the excerpts at length. Format the answer as a bulleted
list, one point per bullet, with a blank line between bullets — use real
line breaks, never the literal characters backslash-n. Cite sources inline
using the bracketed number shown before each excerpt, e.g. [1]. If the
excerpts don't fully answer the question, say so explicitly rather than
guessing.

QUESTION: {question}

EXCERPTS:
{chr(10).join(context_chunks)}
"""
    answer_text = complete(session, project.active_model, synthesis_prompt)
    answer_text = _normalize_answer_text(answer_text)
    latency_ms = int((time.time() - start) * 1000)

    citations = [
        {
            "number": doc_numbers[name],
            "file_name": name,
            "url": doc_urls[name],
            "doc_id": doc_id_by_name[name],
            "node_id": node_id_by_name[name],
        }
        for name in file_names_sorted
    ]

    result = AnswerResult(
        answer=answer_text,
        cited_docs=citations,
        nodes_visited=node_ids,
    )

    _log_query(session, project, question, query_hash, result, latency_ms)
    log_event(logger, "QUERY", project.project_code,
              nodes=len(node_ids), latency_ms=latency_ms,
              scoped=bool(restrict_to_doc_ids))

    return result


def _validate_question(question: str):
    if not question or not question.strip():
        raise ValueError("Question cannot be empty")
    if len(question) > 2000:
        raise ValueError("Question too long (max 2000 chars)")


def _vector_search_section_ids(session, question: str, schema: str,
                               doc_ids: Optional[List[int]] = None) -> List[int]:
    """
    Finds sections whose embedding is semantically closest to the
    question — project-wide, or scoped to doc_ids when given (used when
    the caller already restricted the search to one contract's family).
    Returns [] gracefully if NODE_EMBEDDING isn't populated yet (e.g.
    before a project's first reindex after this feature was added — all
    NULL, nothing to compare against) or AI_EMBED isn't available on this
    account: vector search is an additive retrieval signal, not a hard
    requirement.
    """
    try:
        scope_clause = ""
        params = [EMBED_MODEL, question]
        if doc_ids:
            placeholders = ", ".join(["?"] * len(doc_ids))
            scope_clause = f"AND DOC_ID IN ({placeholders})"
            params = params + list(doc_ids)
        rows = session.sql(
            f"""SELECT NODE_ID
                FROM {schema}.DOCUMENT_INDEX
                WHERE NODE_LEVEL = 'section' AND NODE_EMBEDDING IS NOT NULL
                {scope_clause}
                ORDER BY VECTOR_COSINE_SIMILARITY(NODE_EMBEDDING, AI_EMBED(?, ?)) DESC
                LIMIT {MAX_VECTOR_CANDIDATES}""",
            params=params,
        ).collect()
        return [r["NODE_ID"] for r in rows]
    except Exception:
        logger.warning("EVENT=VECTOR_SEARCH_FAILED", exc_info=True)
        return []


def _keyword_fallback_doc_ids(session, project: ProjectConfig, question: str, schema: str) -> List[int]:
    """
    Extracts literal search terms (clause numbers, defined terms, quoted
    phrases) from the question and searches RAW_TEXT for them directly —
    a complement to summary-based routing, not a replacement: a document
    summary is a compression that can't enumerate every clause number/term
    it contains, so a question asking for one by name needs an exact-text
    search, not a "does this summary sound relevant" judgment. Returns []
    on any failure (extraction or search) so the caller's existing
    "couldn't find a relevant document" message still applies.
    """
    extract_prompt = f"""Extract up to 3 short literal search terms from this
question — specific clause numbers, defined terms, or quoted phrases
someone would search for verbatim in a document. Skip generic/common
words. If there are no such specific terms, return an empty list.

QUESTION: {question}

Return ONLY JSON: {{"terms": ["term1", "term2"]}}"""
    try:
        extracted = complete_json(session, project.active_model, extract_prompt)
    except CortexError:
        logger.warning("EVENT=KEYWORD_FALLBACK_EXTRACT_FAILED question=%r", question)
        return []

    terms = [t.strip() for t in extracted.get("terms", []) if t and t.strip()]
    if not terms:
        return []

    conditions = " OR ".join(["RAW_TEXT ILIKE ?"] * len(terms))
    params = [f"%{t}%" for t in terms]
    rows = session.sql(
        f"""SELECT DISTINCT DOC_ID FROM {schema}.RAW_DOCUMENTS
            WHERE {conditions} LIMIT {MAX_KEYWORD_FALLBACK_DOCS}""",
        params=params,
    ).collect()
    doc_ids = [r["DOC_ID"] for r in rows]
    if doc_ids:
        log_event(logger, "KEYWORD_FALLBACK_HIT", project.project_code,
                  terms=terms, doc_count=len(doc_ids))
    return doc_ids


def _check_cache(session, project: ProjectConfig, query_hash: str) -> Optional[AnswerResult]:
    rows = session.sql(
        f"""SELECT FINAL_ANSWER, CITED_DOCS, NODES_VISITED
            FROM {DATABASE}.{CATALOG_SCHEMA}.PROJECT_QUERY_LOG
            WHERE PROJECT_ID = (SELECT PROJECT_ID FROM {DATABASE}.{CATALOG_SCHEMA}.PROJECTS
                                 WHERE PROJECT_CODE = ?)
              AND QUERY_HASH = ?
              AND CREATED_AT > DATEADD(HOUR, -?, CURRENT_TIMESTAMP())
            ORDER BY CREATED_AT DESC LIMIT 1""",
        params=[project.project_code, query_hash, project.query_cache_ttl_hours],
    ).collect()
    if not rows:
        return None
    r = rows[0]
    import json
    return AnswerResult(
        answer=r["FINAL_ANSWER"],
        cited_docs=json.loads(r["CITED_DOCS"]) if r["CITED_DOCS"] else [],
        nodes_visited=json.loads(r["NODES_VISITED"]) if r["NODES_VISITED"] else [],
        from_cache=True,
    )


def _log_query(session, project: ProjectConfig, question: str, query_hash: str,
              result: AnswerResult, latency_ms: int):
    import json
    session.sql(
        f"""INSERT INTO {DATABASE}.{CATALOG_SCHEMA}.PROJECT_QUERY_LOG
            (PROJECT_ID, USER_QUESTION, QUERY_HASH, NODES_VISITED, FINAL_ANSWER,
             CITED_DOCS, LATENCY_MS)
            SELECT (SELECT PROJECT_ID FROM {DATABASE}.{CATALOG_SCHEMA}.PROJECTS
                    WHERE PROJECT_CODE = ?), ?, ?, PARSE_JSON(?), ?, PARSE_JSON(?), ?""",
        params=[project.project_code, question, query_hash,
                json.dumps(result.nodes_visited), result.answer,
                json.dumps(result.cited_docs), latency_ms],
    ).collect()
