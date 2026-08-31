"""
index_builder.py — builds the hierarchical DOCUMENT_INDEX tree for newly
ingested documents.

Forked from the project-llm-wiki template,
which keys the prompt off ProjectConfig.segmentation_profile so a new
project either reuses 'GENERIC' or adds a specialized entry to PROMPTS
without touching the calling code — this file adds 'LEX_CONTRACT' as one
such entry (see PROMPTS below).

Difference from that template: _index_one_document now CHUNKS documents
longer than project.max_document_chars instead of truncating them. The
original template did `text = raw_text[:project.max_document_chars]` before
a single indexing call — fine for meeting minutes (a few pages), but
signed contracts run 100-500 pages, and 150,000 characters is only around
50 pages. A contract past that length would have silently lost everything
beyond it from the index. See _index_one_document's docstring for how
chunking works.
"""

from dataclasses import dataclass, field
from typing import List, Optional

from config import ProjectConfig
from utils.cortex_client import complete, complete_json
from utils.logging_utils import get_logger, log_event

logger = get_logger(__name__)

# Must stay <= DOCUMENT_INDEX.NODE_SUMMARY/NODE_TITLE's actual column
# widths (see sql/00_setup_catalog.sql) — a defensive backstop, not the
# primary control: a segmentation prompt asking for detailed summaries
# can produce a response long enough to fail the INSERT outright ("... is
# too long and would be truncated", which Snowflake raises as an error
# rather than silently truncating) instead of just losing a few words off
# the end. Truncating here first means a verbose response degrades
# gracefully instead of failing the document entirely.
MAX_NODE_SUMMARY_CHARS = 8000
MAX_NODE_TITLE_CHARS = 500

# Text embedding model for DOCUMENT_INDEX.NODE_EMBEDDING (vector/semantic
# search — see query_engine.py's hybrid retrieval). AI_EMBED is the
# forward-looking function (EMBED_TEXT_768/1024 are the legacy names,
# slated for deprecation); 'snowflake-arctic-embed-m' returns a 768-dim
# vector, matching DOCUMENT_INDEX.NODE_EMBEDDING's declared width.
EMBED_MODEL = "snowflake-arctic-embed-m"


def _truncate(text, max_chars: int) -> str:
    text = text or ""
    return text[:max_chars]


@dataclass
class IndexResult:
    indexed: int
    failed: int
    errors: List[str] = field(default_factory=list)  # "file_name: error message"

PROMPTS = {
    "GENERIC": """You are indexing a document into a navigable tree structure.
Read the document text below and identify its natural sections (headings,
topic breaks, or logical divisions — do not force a fixed set of section
names). For each section, produce a concise 2-3 sentence summary plus the
character offsets (start, end) of that section within the original text.
{granularity_instruction}
Return ONLY valid JSON in this shape, no commentary:
{{
  "document_summary": "2-3 sentence summary of the whole document",
  "sections": [
    {{"title": "...", "summary": "...", "start": 0, "end": 1234}}
  ]
}}

DOCUMENT TEXT:
{text}
""",

    # LEX's segmentation profile: contracts are clause/schedule-structured
    # legal text, not prose with generic topic breaks (GENERIC) or dated
    # meetings (the template's original ORG_MEETING_MINUTES profile,
    # not carried over here — it doesn't apply to this project's content).
    "LEX_CONTRACT": """You are indexing a signed contract (or one of its
variations, extensions, or novation deeds) into a navigable tree structure.
Segment by the contract's own structure — clauses, sub-clauses, schedules,
annexures, and appendices — rather than forcing generic topic breaks; use
whatever heading/numbering scheme the document itself uses.

Each section's summary should be detailed enough for a reader to judge
relevance without re-reading the source: state what the clause/schedule
covers and its practical effect. Quote clause numbers, defined terms,
dollar figures, dates, and notice periods VERBATIM exactly as written in
the source (e.g. "Clause 14.3", "Bank Guarantee", "$250,000", "90 days")
rather than paraphrasing or omitting them — retrieval accuracy for
questions like "what is the notice period for termination" depends on the
exact figure appearing in the section summary itself, not just its general
meaning. If a clause explicitly cross-references another clause/schedule
in this same document, note that cross-reference in the summary.

Do not force sections that don't exist in the text; if the document has no
clear clause numbering, fall back to its natural headings instead.
{granularity_instruction}
Return ONLY valid JSON in this shape, no commentary:
{{
  "document_summary": "3-5 sentence summary of the whole document (what it is, the parties, its overall purpose and scope)",
  "sections": [
    {{"title": "e.g. 'Clause 14.3 — Termination for Convenience' or 'Schedule 2 — Pricing'", "summary": "...", "start": 0, "end": 1234}}
  ]
}}

DOCUMENT TEXT:
{text}
""",

    # Add further project-specific profiles here, e.g. 'MEETING_MINUTES', and
    # set PROJECTS.SEGMENTATION_PROFILE to match.
}

# Optional per-project knob (PROJECTS.SEGMENTATION_GRANULARITY): pushes the
# indexing prompt to split each natural break further into per-topic /
# per-clause sections instead of one section per heading. Both PROMPTS
# templates above take a {granularity_instruction} placeholder so any
# segmentation profile can run at either granularity.
SEGMENTATION_GRANULARITY_INSTRUCTIONS = {
    "STANDARD": "",
    "DETAILED": (
        "\nGo finer-grained than one section per top-level heading: within "
        "each clause or schedule, further split into one section per "
        "distinct sub-clause or topic discussed, so each section covers a "
        "single provision rather than an entire clause. This matters for "
        "precise citation — a citation pointing at 'Clause 14' in a "
        "50-page contract is far less useful than one pointing at 'Clause "
        "14.3'.\n"
    ),
}


def _granularity_instruction(project: ProjectConfig) -> str:
    return SEGMENTATION_GRANULARITY_INSTRUCTIONS.get(
        project.segmentation_granularity, ""
    )


# The indexing response's JSON grows with how many sections a document
# gets split into — complete_json()'s 4096-token default (sized for
# shorter, more conversational chat responses) is routinely too small
# once a document has many clauses/schedules, and far too small under
# DETAILED granularity, which deliberately asks for more, finer-grained
# sections. Indexing is a batch job, not a latency-sensitive per-question
# call, so there's no real cost to budgeting generously here — complete_json()
# still retries with an even larger budget on its own if a response gets
# truncated anyway.
INDEXING_MAX_TOKENS = {
    "STANDARD": 8192,
    "DETAILED": 12000,
}


def _indexing_max_tokens(project: ProjectConfig) -> int:
    return INDEXING_MAX_TOKENS.get(project.segmentation_granularity, 8192)


def build_index_for_project(session, project: ProjectConfig,
                             doc_ids: Optional[List[int]] = None,
                             rebuild: bool = False) -> IndexResult:
    """
    doc_ids=None -> index every document not yet in DOCUMENT_INDEX (or every
    document if rebuild=True).

    Each document is indexed independently — one document's LLM response
    coming back malformed (e.g. not the requested JSON shape) is recorded
    as a per-document failure, not an exception that aborts every other
    document still queued in the same call.
    """
    schema = project.qualified_schema
    prompt_template = PROMPTS.get(project.segmentation_profile, PROMPTS["GENERIC"])

    if doc_ids:
        placeholders = ", ".join(["?"] * len(doc_ids))
        where = f"DOC_ID IN ({placeholders})"
        params = doc_ids
    elif rebuild:
        where = "1=1"
        params = []
    else:
        where = f"""DOC_ID NOT IN (
            SELECT DISTINCT DOC_ID FROM {schema}.DOCUMENT_INDEX
        )"""
        params = []

    docs = session.sql(
        f"SELECT DOC_ID, FILE_NAME, RAW_TEXT FROM {schema}.RAW_DOCUMENTS WHERE {where}",
        params=params,
    ).collect()

    # Shared across the whole run (not reset per-document): if AI_EMBED
    # fails once — e.g. embeddings aren't enabled on this account — there's
    # no reason to retry and fail identically on every remaining document.
    # A single mutable cell so _index_one_document can flip it off and have
    # that take effect for the rest of this call. Embeddings are an
    # additive retrieval signal (see query_engine.py), not a hard
    # requirement, so disabling them never fails the document itself.
    embed_enabled = [True]

    errors: List[str] = []
    indexed = 0
    for doc in docs:
        try:
            _index_one_document(session, project, doc["DOC_ID"], doc["FILE_NAME"],
                                 doc["RAW_TEXT"], prompt_template, rebuild, embed_enabled)
            indexed += 1
        except Exception as e:  # noqa: BLE001
            errors.append(f"{doc['FILE_NAME']}: {e}")

    return IndexResult(indexed=indexed, failed=len(errors), errors=errors)


def _chunk_text(text: str, chunk_size: int) -> List[tuple]:
    """Splits text into (chunk_start_offset, chunk_text) pairs of at most
    chunk_size characters each. A document shorter than chunk_size is
    returned as a single chunk starting at offset 0 — the common case for
    most documents, which behaves identically to the pre-chunking code
    path (one indexing call, no extra summary-merge call)."""
    if not text:
        return [(0, "")]
    return [(i, text[i:i + chunk_size]) for i in range(0, len(text), chunk_size)]


# A chunked document's per-chunk "document_summary" values are each only a
# summary of PART of the document (whatever that chunk happened to
# contain) — not usable as the document's actual summary on their own.
# This merge call is cheap (short prompt, short expected output) and only
# runs for documents that actually needed more than one chunk.
_MERGE_SUMMARIES_PROMPT = """The following are summaries of consecutive
parts of one longer document, in order. Write a single 3-5 sentence
summary of the WHOLE document from these parts (what it is, the parties
if named, its overall purpose and scope) — do not describe it as "part 1
covers X, part 2 covers Y", just synthesize one coherent overview.

PART SUMMARIES:
{part_summaries}
"""


def _index_one_document(session, project: ProjectConfig, doc_id: int, file_name: str,
                         raw_text: str, prompt_template: str, rebuild: bool,
                         embed_enabled: List[bool]):
    """
    Indexes one document, chunking it first if it's longer than
    project.max_document_chars (repurposed from "hard truncation cutoff"
    in the original template to "chunk size" here — the column's meaning
    is now "how much text one indexing LLM call can safely handle", which
    both readings agree on; the difference is what happens to the text
    PAST that size: dropped, vs. handled in a second call).

    Every chunk's sections are inserted as siblings under the same single
    'document'-level root node — from query_engine.py's point of view a
    document indexed from 3 chunks looks identical to one indexed from 1
    chunk, since both just produce a set of 'section' nodes under one
    'document' node. The only new bookkeeping is translating each
    section's chunk-relative (start, end) offsets into offsets against the
    FULL raw_text, so NODE_TEXT_REF still means what query_engine.py
    already assumes it means (an offset pair into RAW_DOCUMENTS.RAW_TEXT).
    """
    schema = project.qualified_schema
    granularity_instruction = _granularity_instruction(project)
    max_tokens = _indexing_max_tokens(project)

    if rebuild:
        session.sql(f"DELETE FROM {schema}.DOCUMENT_INDEX WHERE DOC_ID = ?",
                    params=[doc_id]).collect()

    try:
        chunks = _chunk_text(raw_text or "", project.max_document_chars)

        chunk_summaries: List[str] = []
        all_sections = []  # [(abs_start, abs_end, title, summary), ...]
        for chunk_start, chunk_text in chunks:
            result = complete_json(session, project.active_model,
                                    prompt_template.format(
                                        text=chunk_text,
                                        granularity_instruction=granularity_instruction,
                                    ),
                                    max_tokens=max_tokens)
            chunk_summaries.append(result.get("document_summary", ""))
            for section in result.get("sections", []):
                title = _truncate(section.get("title", ""), MAX_NODE_TITLE_CHARS)
                summary = _truncate(section.get("summary", ""), MAX_NODE_SUMMARY_CHARS)
                sec_start = section.get("start", 0)
                sec_end = section.get("end", len(chunk_text))
                all_sections.append((chunk_start + sec_start, chunk_start + sec_end, title, summary))

        if len(chunks) == 1:
            document_summary = chunk_summaries[0] if chunk_summaries else ""
        else:
            document_summary = complete(
                session, project.active_model,
                _MERGE_SUMMARIES_PROMPT.format(
                    part_summaries="\n\n".join(
                        f"Part {i + 1}: {s}" for i, s in enumerate(chunk_summaries) if s
                    )
                ),
                max_tokens=500,
            )
            log_event(logger, "INDEX_CHUNKED", project.project_code,
                      doc_id=doc_id, chunks=len(chunks))

        session.sql(
            f"""INSERT INTO {schema}.DOCUMENT_INDEX
                (DOC_ID, PARENT_NODE_ID, NODE_LEVEL, NODE_TITLE, NODE_SUMMARY, NODE_TEXT_REF)
                SELECT ?, NULL, 'document', ?, ?, ?""",
            params=[doc_id, _truncate(file_name, MAX_NODE_TITLE_CHARS),
                    _truncate(document_summary, MAX_NODE_SUMMARY_CHARS),
                    f"0:{len(raw_text or '')}"],
        ).collect()
        root_node_id = session.sql(
            f"""SELECT NODE_ID FROM {schema}.DOCUMENT_INDEX
                WHERE DOC_ID = ? AND PARENT_NODE_ID IS NULL
                ORDER BY NODE_ID DESC LIMIT 1""",
            params=[doc_id],
        ).collect()[0]["NODE_ID"]

        for abs_start, abs_end, title, summary in all_sections:
            text_ref = f"{abs_start}:{abs_end}"
            inserted = False
            if project.enable_vector_search and embed_enabled[0]:
                try:
                    session.sql(
                        f"""INSERT INTO {schema}.DOCUMENT_INDEX
                            (DOC_ID, PARENT_NODE_ID, NODE_LEVEL, NODE_TITLE, NODE_SUMMARY,
                             NODE_TEXT_REF, NODE_EMBEDDING)
                            SELECT ?, ?, 'section', ?, ?, ?, AI_EMBED(?, ?)""",
                        params=[doc_id, root_node_id, title, summary, text_ref,
                                EMBED_MODEL, f"{title}: {summary}"],
                    ).collect()
                    inserted = True
                except Exception:
                    logger.warning(
                        "EVENT=EMBED_UNAVAILABLE — disabling embeddings for the "
                        "rest of this run, indexing continues without them",
                        exc_info=True,
                    )
                    embed_enabled[0] = False

            if not inserted:
                session.sql(
                    f"""INSERT INTO {schema}.DOCUMENT_INDEX
                        (DOC_ID, PARENT_NODE_ID, NODE_LEVEL, NODE_TITLE, NODE_SUMMARY, NODE_TEXT_REF)
                        SELECT ?, ?, 'section', ?, ?, ?""",
                    params=[doc_id, root_node_id, title, summary, text_ref],
                ).collect()

        log_event(logger, "INDEX_BUILD", project.project_code,
                  doc_id=doc_id, sections=len(all_sections), chunks=len(chunks))

    except Exception:
        logger.exception("EVENT=INDEX_BUILD_ERROR doc_id=%s file=%s", doc_id, file_name)
        raise
