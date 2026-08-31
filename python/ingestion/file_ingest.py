"""
file_ingest.py — ingest user-uploaded files into RAW_DOCUMENTS.

Unchanged from the project-llm-wiki template.
This is the "no mandatory pre-step" path: a user drops files straight into
the Streamlit Data Sources page and this runs synchronously (slower per file
than a batch job, but nothing needs to exist before the app is usable).
"""

import hashlib
from dataclasses import dataclass
from typing import List

from config import ProjectConfig, MIN_PARSED_TEXT_CHARS
from ingestion.xlsx_parser import is_xlsx, parse_xlsx_to_text
from utils.logging_utils import get_logger, log_event
from utils.sql_utils import SQLBuilder

logger = get_logger(__name__)


@dataclass
class IngestResult:
    file_name: str
    status: str          # 'INGESTED' | 'SKIPPED_DUPLICATE' | 'FAILED'
    doc_id: int = None
    error: str = None


def ingest_uploaded_files(session, project: ProjectConfig, uploaded_files: List) -> List[IngestResult]:
    """
    uploaded_files: list of Streamlit UploadedFile objects (has .name, .read()).
    Stages each file, parses it with AI_PARSE_DOCUMENT, and merges into
    RAW_DOCUMENTS (idempotent — a byte-identical re-upload is a no-op).
    """
    results: List[IngestResult] = []
    schema = project.qualified_schema
    stage = project.qualified_stage

    for f in uploaded_files:
        try:
            raw_bytes = f.read()
            stage_path = f"{stage}/{f.name}"

            # PUT requires a local file path in Snowpark; write to a temp
            # location first via the session's file put_stream for in-memory bytes.
            session.file.put_stream(
                _to_stream(raw_bytes), stage_path, auto_compress=False, overwrite=True
            )

            if is_xlsx(f.name):
                raw_text = parse_xlsx_to_text(raw_bytes)
            else:
                # AI_PARSE_DOCUMENT needs a FILE-typed argument, built via
                # TO_FILE('@stage', 'path') — not the VARCHAR URL
                # BUILD_SCOPED_FILE_URL returns (same bug fixed in
                # sharepoint_ingest.py's equivalent call).
                parsed = session.sql(
                    "SELECT AI_PARSE_DOCUMENT(TO_FILE(?, ?), "
                    "PARSE_JSON('{\"mode\": \"OCR\"}')) AS RESULT",
                    params=[f"@{stage}", f.name],
                ).collect()
                raw_text = _extract_text(parsed[0]["RESULT"])

            if len(raw_text.strip()) < MIN_PARSED_TEXT_CHARS:
                results.append(IngestResult(f.name, "FAILED",
                                             error="Parsed text too short — file may be scanned/empty"))
                continue

            source_hash = hashlib.sha256(raw_text.encode()).hexdigest()

            before = session.sql(
                f"SELECT COUNT(*) AS C FROM {schema}.RAW_DOCUMENTS WHERE SOURCE_HASH = ?",
                params=[source_hash],
            ).collect()[0]["C"]

            session.sql(
                SQLBuilder.build_merge_raw_document(schema),
                params=[f.name, stage_path, "UPLOAD", None, None, raw_text,
                        source_hash, None],
            ).collect()

            if before > 0:
                results.append(IngestResult(f.name, "SKIPPED_DUPLICATE"))
            else:
                doc_id = session.sql(
                    f"SELECT DOC_ID FROM {schema}.RAW_DOCUMENTS WHERE SOURCE_HASH = ?",
                    params=[source_hash],
                ).collect()[0]["DOC_ID"]
                results.append(IngestResult(f.name, "INGESTED", doc_id=doc_id))

            log_event(logger, "INGEST_FILE", project.project_code,
                      file=f.name, status=results[-1].status)

        except Exception as e:  # noqa: BLE001
            logger.exception("EVENT=INGEST_FILE_ERROR file=%s", f.name)
            results.append(IngestResult(f.name, "FAILED", error=str(e)))

    _log_sync_run(session, project, "UPLOAD", results)
    return results


def _to_stream(raw_bytes: bytes):
    import io
    return io.BytesIO(raw_bytes)


def _extract_text(parse_result) -> str:
    """AI_PARSE_DOCUMENT returns a VARIANT; pull the plain-text content out of it."""
    import json
    data = json.loads(parse_result) if isinstance(parse_result, str) else parse_result
    return data.get("content", "") if isinstance(data, dict) else str(data)


def _log_sync_run(session, project: ProjectConfig, source_type: str, results: List[IngestResult]):
    from config import DATABASE, CATALOG_SCHEMA
    ingested = sum(1 for r in results if r.status == "INGESTED")
    skipped = sum(1 for r in results if r.status == "SKIPPED_DUPLICATE")
    failed = sum(1 for r in results if r.status == "FAILED")
    session.sql(
        f"""INSERT INTO {DATABASE}.{CATALOG_SCHEMA}.PROJECT_SYNC_LOG
            (PROJECT_ID, SOURCE_TYPE, FILES_FOUND, FILES_SYNCED, FILES_SKIPPED, FILES_FAILED)
            SELECT (SELECT PROJECT_ID FROM {DATABASE}.{CATALOG_SCHEMA}.PROJECTS
                    WHERE PROJECT_CODE = ?), ?, ?, ?, ?, ?""",
        params=[project.project_code, source_type, len(results), ingested, skipped, failed],
    ).collect()
