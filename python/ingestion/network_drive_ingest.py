"""
network_drive_ingest.py — list & ingest files from LEX's network drive
share (SMB) — the contracts team's actual "network drive" (confirmed not
to be SharePoint). Two-step, both driven from the Streamlit Data Sources
page:

  1. list_network_drive_folder(project, subfolder) -> [NetworkDriveItem, ...] for checkboxes
  2. ingest_selected_files(session, project, items) -> [IngestResult, ...]

Forked from this template's usual SharePoint/Graph API ingestion module,
replaced end-to-end with SMB via utils/network_drive_client.py — see that
module's docstring for the "UNVERIFIED" caveat, which applies here too.

Every selected item is re-downloaded and re-parsed on each run (not just
new ones) so that an edit to an already-ingested file is detected — the
per-item SOURCE_ITEM_ID (the file's UNC path) identifies the same logical
document across edits, and ingest_selected_files updates its existing
RAW_DOCUMENTS row in place (status "UPDATED") when the freshly-parsed
content's hash differs from what's stored, rather than treating every run
as either brand-new or an unchanged duplicate.
"""

import hashlib
from dataclasses import dataclass
from typing import List

from config import ProjectConfig, MIN_PARSED_TEXT_CHARS
from ingestion.xlsx_parser import is_xlsx, parse_xlsx_to_text
from utils import network_drive_client
from utils.logging_utils import get_logger, log_event
from utils.sql_utils import SQLBuilder

logger = get_logger(__name__)


@dataclass
class IngestResult:
    file_name: str
    status: str          # 'INGESTED' | 'UPDATED' | 'SKIPPED_DUPLICATE' | 'FAILED'
    doc_id: int = None
    error: str = None


def list_network_drive_folder(project: ProjectConfig, subfolder: str = "") -> List[network_drive_client.NetworkDriveItem]:
    """Lists files under the share's default path (or an explicit subfolder), recursively."""
    return network_drive_client.list_files(project, relative_path=subfolder, recursive=True)


def ingest_selected_files(session, project: ProjectConfig,
                          selected_items: List[network_drive_client.NetworkDriveItem]) -> List[IngestResult]:
    results: List[IngestResult] = []
    schema = project.qualified_schema
    stage = project.qualified_stage

    for item in selected_items:
        try:
            # Keyed on the file's UNC path (a stable per-file identity, not
            # a content hash) so a file that's been edited since it was
            # last ingested is detected and updated in place, rather than
            # skipped as an unchanged duplicate.
            existing = session.sql(
                f"SELECT DOC_ID, SOURCE_HASH FROM {schema}.RAW_DOCUMENTS WHERE SOURCE_ITEM_ID = ?",
                params=[item.item_id],
            ).collect()

            raw_bytes = network_drive_client.download_file(project, item.item_id)
            stage_path = f"{stage}/{item.name}"
            session.file.put_stream(_to_stream(raw_bytes), stage_path,
                                    auto_compress=False, overwrite=True)

            if is_xlsx(item.name):
                raw_text = parse_xlsx_to_text(raw_bytes)
            else:
                # AI_PARSE_DOCUMENT takes a FILE-typed argument, built via
                # TO_FILE('@stage', 'relative_path') — not the VARCHAR URL
                # BUILD_SCOPED_FILE_URL returns ("Invalid argument types
                # for function 'AI_PARSE_DOCUMENT$V4': (VARCHAR, VARIANT)"
                # when given one). Unlike BUILD_SCOPED_FILE_URL, TO_FILE's
                # stage argument is an ordinary quoted string, so both
                # arguments can be bind parameters here.
                parsed = session.sql(
                    "SELECT AI_PARSE_DOCUMENT(TO_FILE(?, ?), "
                    "PARSE_JSON('{\"mode\": \"OCR\"}')) AS RESULT",
                    params=[f"@{stage}", item.name],
                ).collect()
                raw_text = _extract_text(parsed[0]["RESULT"])

            if len(raw_text.strip()) < MIN_PARSED_TEXT_CHARS:
                results.append(IngestResult(item.name, "FAILED",
                                             error="Parsed text too short"))
                continue

            source_hash = hashlib.sha256(raw_text.encode()).hexdigest()

            if existing and existing[0]["SOURCE_HASH"] == source_hash:
                results.append(IngestResult(item.name, "SKIPPED_DUPLICATE",
                                             doc_id=existing[0]["DOC_ID"]))
                continue

            # No web-browsable URL for a network-drive file the way
            # SharePoint's webUrl worked (a UNC path isn't a clickable web
            # link, and a remote browser has no route to the internal file
            # server anyway) — SOURCE_URL stays NULL; citations render as
            # plain filenames and rely on citation_panel_ui.py's presigned
            # Snowflake stage URL instead.
            session.sql(
                SQLBuilder.build_merge_raw_document_by_source_item(schema),
                params=[item.name, stage_path, "NETWORK_DRIVE", item.item_id,
                        None, raw_text, source_hash, None],
            ).collect()

            doc_id = session.sql(
                f"SELECT DOC_ID FROM {schema}.RAW_DOCUMENTS WHERE SOURCE_ITEM_ID = ?",
                params=[item.item_id],
            ).collect()[0]["DOC_ID"]
            status = "UPDATED" if existing else "INGESTED"
            results.append(IngestResult(item.name, status, doc_id=doc_id))

            log_event(logger, "INGEST_NETWORK_DRIVE_FILE", project.project_code,
                      file=item.name, status=status)

        except Exception as e:  # noqa: BLE001
            logger.exception("EVENT=INGEST_NETWORK_DRIVE_ERROR file=%s", item.name)
            results.append(IngestResult(item.name, "FAILED", error=str(e)))

    _log_sync_run(session, project, results)
    return results


def _to_stream(raw_bytes: bytes):
    import io
    return io.BytesIO(raw_bytes)


def _extract_text(parse_result) -> str:
    import json
    data = json.loads(parse_result) if isinstance(parse_result, str) else parse_result
    return data.get("content", "") if isinstance(data, dict) else str(data)


def _log_sync_run(session, project: ProjectConfig, results: List[IngestResult]):
    from config import DATABASE, CATALOG_SCHEMA
    ingested = sum(1 for r in results if r.status in ("INGESTED", "UPDATED"))
    skipped = sum(1 for r in results if r.status == "SKIPPED_DUPLICATE")
    failed = sum(1 for r in results if r.status == "FAILED")
    session.sql(
        f"""INSERT INTO {DATABASE}.{CATALOG_SCHEMA}.PROJECT_SYNC_LOG
            (PROJECT_ID, SOURCE_TYPE, FILES_FOUND, FILES_SYNCED, FILES_SKIPPED, FILES_FAILED)
            SELECT (SELECT PROJECT_ID FROM {DATABASE}.{CATALOG_SCHEMA}.PROJECTS
                    WHERE PROJECT_CODE = ?), 'NETWORK_DRIVE', ?, ?, ?, ?""",
        params=[project.project_code, len(results), ingested, skipped, failed],
    ).collect()
