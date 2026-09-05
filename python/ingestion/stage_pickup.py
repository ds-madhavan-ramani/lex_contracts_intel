"""
stage_pickup.py — pick up files staged into NETWORK_DRIVE_INBOX_STAGE by
the companion lex_network_bridge repo's bridge tool (a Linux host running
*inside* the MTM network, since Snowflake can't yet reach the network
drive directly — see sql/test_network_drive_connectivity.sql) and feed
them into this app's normal ingest pipeline: RAW_DOCUMENTS, contract
linking, and indexing — the same three steps
ingestion/network_drive_ingest.py's ingest_selected_files() already does
for a direct-SMB source, just sourcing the file from a different place.

Staging convention (see lex_network_bridge's network_drive_to_stage.py):
files land at @NETWORK_DRIVE_INBOX_STAGE/<CW_NUMBER>/<filename> — the CW
folder a human explicitly searched/selected in the bridge's browser app
(or a best-effort regex fallback for its CLI), not a guess made here.
That is what makes automatic CONTRACT_REGISTER linking safe in this one
module specifically: contract_linking.suggest_cw_number() is deliberately
never auto-applied anywhere else in this codebase ("a wrong auto-link
would silently merge two unrelated contracts' answers together, a
correctness risk this template treats as unacceptable for a legal tool")
— but the CW here comes from a folder a person already confirmed by
searching it, not a regex guess against a file's name or content.

No file bytes ever pass through this process's memory: the file is
already sitting in Snowflake (in NETWORK_DRIVE_INBOX_STAGE), so it's
copied stage-to-stage with COPY FILES (server-side, no GET/PUT round
trip) into the project's own stage, then parsed with AI_PARSE_DOCUMENT
exactly like the upload/SMB ingest paths — this is what makes the picked-
up file work with citation_viewer.py's presigned-URL original-document
view unmodified, since that only ever looks at project.qualified_stage.

Called from a scheduled Snowflake Task (see sql/04_stage_pickup_task.sql)
via run_stage_pickup(), but every function here also runs standalone from
a notebook cell or worksheet for manual/ad hoc use.

DOC_ROLE heuristic: a file's role (BASE vs. VARIATION/EXTENSION/etc.)
can't be determined automatically from a bare filename in general, so the
first document linked to a given contract in a run is recorded as BASE
and every subsequent one as VARIATION — a reasonable default (one base
agreement plus later amendments is the common real-world shape) but a
guess, not a certainty; review/correct roles on the Contract Register
page same as any manually-linked document.

UNVERIFIED: written without a live Snowflake account, Task, or Stream to
test against. COPY FILES INTO <stage> FROM <stage> (stage-to-stage, no
Python byte handling) is assumed to behave the same way GET/PUT does
elsewhere in this codebase; if it doesn't, fall back to the
download-bytes-then-put_stream pattern network_drive_ingest.py already
uses successfully (GET the bytes from NETWORK_DRIVE_INBOX_STAGE instead
of downloading over SMB, then put_stream into project.qualified_stage).
"""

import hashlib
import json
from dataclasses import dataclass
from typing import List, Optional, Set

from config import ProjectConfig, MIN_PARSED_TEXT_CHARS
import contract_linking
from ingestion.xlsx_parser import is_xlsx, parse_xlsx_to_text
from utils.logging_utils import get_logger, log_event
from utils.sql_utils import SQLBuilder

logger = get_logger(__name__)

INBOX_STAGE = "MEDSCOMA.DATA_LEX.NETWORK_DRIVE_INBOX_STAGE"


@dataclass
class StagedFile:
    cw_number: str
    relative_path: str   # "<CW>/<filename>" — relative to INBOX_STAGE; the SOURCE_ITEM_ID dedup key
    file_name: str


@dataclass
class PickupResult:
    file_name: str
    cw_number: str
    status: str    # 'INGESTED' | 'UPDATED' | 'SKIPPED_DUPLICATE' | 'FAILED'
    doc_id: Optional[int] = None
    contract_id: Optional[int] = None
    error: Optional[str] = None


def list_staged_files(session) -> List[StagedFile]:
    """Lists every file currently sitting in NETWORK_DRIVE_INBOX_STAGE via
    its directory table (see sql/04_stage_pickup_task.sql — DIRECTORY must
    be enabled on the stage), reading the CW number off each file's
    immediate parent folder. A file sitting at the stage root with no CW
    subfolder (an older or malformed upload) is skipped with a warning
    rather than guessed at — same principle as the module docstring's
    auto-linking note: only proceed when the CW is known with certainty."""
    rows = session.sql(f"SELECT RELATIVE_PATH FROM DIRECTORY(@{INBOX_STAGE})").collect()
    staged: List[StagedFile] = []
    for row in rows:
        relative_path = row["RELATIVE_PATH"]
        if "/" not in relative_path:
            logger.warning("EVENT=STAGE_PICKUP_NO_CW_FOLDER path=%s", relative_path)
            continue
        cw_number, file_name = relative_path.split("/", 1)
        if "/" in file_name:
            # Nested more than one level deep — not the expected
            # <CW>/<filename> shape. Skip rather than guess which segment
            # is the real CW folder.
            logger.warning("EVENT=STAGE_PICKUP_UNEXPECTED_NESTING path=%s", relative_path)
            continue
        staged.append(StagedFile(cw_number=cw_number, relative_path=relative_path, file_name=file_name))
    return staged


def pick_up_staged_files(session, project: ProjectConfig, staged: List[StagedFile]) -> List[PickupResult]:
    results: List[PickupResult] = []
    schema = project.qualified_schema
    stage = project.qualified_stage

    # How many documents each contract already has linked, going into this
    # run — drives the BASE-then-VARIATION heuristic (see module docstring).
    linked_counts = {}

    for item in staged:
        try:
            existing = session.sql(
                f"SELECT DOC_ID, SOURCE_HASH FROM {schema}.RAW_DOCUMENTS WHERE SOURCE_ITEM_ID = ?",
                params=[item.relative_path],
            ).collect()

            dest_path = f"{stage}/{item.file_name}"
            # Server-side stage-to-stage copy — no bytes pass through this
            # process. See module docstring's UNVERIFIED note.
            session.sql(
                f"COPY FILES INTO @{stage}/ FROM @{INBOX_STAGE}/{item.cw_number}/ FILES = (?)",
                params=[item.file_name],
            ).collect()

            if is_xlsx(item.file_name):
                # Not expected in this pipeline (contract PDFs only), but
                # handled the same way the other two ingest paths do in
                # case a register workbook ever lands here by mistake.
                raw_bytes = session.file.get_stream(f"@{stage}/{item.file_name}").read()
                raw_text = parse_xlsx_to_text(raw_bytes)
            else:
                parsed = session.sql(
                    "SELECT AI_PARSE_DOCUMENT(TO_FILE(?, ?), "
                    "PARSE_JSON('{\"mode\": \"OCR\"}')) AS RESULT",
                    params=[f"@{stage}", item.file_name],
                ).collect()
                raw_text = _extract_text(parsed[0]["RESULT"])

            if len(raw_text.strip()) < MIN_PARSED_TEXT_CHARS:
                results.append(PickupResult(item.file_name, item.cw_number, "FAILED",
                                             error="Parsed text too short"))
                continue

            # Hashed on parsed text, not file bytes — matches
            # network_drive_ingest.py exactly, and means a file whose
            # content is unchanged is recognized as such even if its raw
            # bytes differ trivially (re-saved PDF metadata, etc.).
            source_hash = hashlib.sha256(raw_text.encode()).hexdigest()

            if existing and existing[0]["SOURCE_HASH"] == source_hash:
                results.append(PickupResult(item.file_name, item.cw_number, "SKIPPED_DUPLICATE",
                                             doc_id=existing[0]["DOC_ID"]))
                continue

            session.sql(
                SQLBuilder.build_merge_raw_document_by_source_item(schema),
                params=[item.file_name, dest_path, "NETWORK_DRIVE_STAGE", item.relative_path,
                        None, raw_text, source_hash, None],
            ).collect()

            doc_id = session.sql(
                f"SELECT DOC_ID FROM {schema}.RAW_DOCUMENTS WHERE SOURCE_ITEM_ID = ?",
                params=[item.relative_path],
            ).collect()[0]["DOC_ID"]

            # Auto-link — safe here specifically because item.cw_number
            # came from the staging path a human already confirmed by
            # searching that CW folder, not a filename guess. See module
            # docstring.
            contract_id = contract_linking.get_or_create_contract(session, project, item.cw_number)
            prior_links = linked_counts.get(contract_id)
            if prior_links is None:
                prior_links = len(contract_linking.list_family_documents(session, project, contract_id))
            doc_role = "BASE" if prior_links == 0 else "VARIATION"
            contract_linking.link_document(session, project, contract_id, doc_id, doc_role)
            linked_counts[contract_id] = prior_links + 1

            status = "UPDATED" if existing else "INGESTED"
            results.append(PickupResult(item.file_name, item.cw_number, status,
                                         doc_id=doc_id, contract_id=contract_id))

            log_event(logger, "STAGE_PICKUP_FILE", project.project_code,
                      file=item.file_name, cw=item.cw_number, status=status, doc_role=doc_role)

        except Exception as e:  # noqa: BLE001 — one bad file shouldn't abort the whole run
            logger.exception("EVENT=STAGE_PICKUP_ERROR file=%s cw=%s", item.file_name, item.cw_number)
            results.append(PickupResult(item.file_name, item.cw_number, "FAILED", error=str(e)))

    _log_sync_run(session, project, results)
    return results


def run_stage_pickup(session, project_code: str = "LEX") -> str:
    """Entry point for the scheduled Task (sql/04_stage_pickup_task.sql)
    and for manual/ad hoc runs (a notebook cell, a worksheet CALL). Lists
    every file currently in NETWORK_DRIVE_INBOX_STAGE, ingests/links/
    indexes each one not already up to date, then runs stock-field
    extraction once per contract actually touched this run — not once per
    file, so a CW folder with several files only needs one extraction
    pass."""
    from config import load_project
    project = load_project(session, project_code)

    staged = list_staged_files(session)
    results = pick_up_staged_files(session, project, staged)

    new_doc_ids = [r.doc_id for r in results if r.status in ("INGESTED", "UPDATED") and r.doc_id]
    if new_doc_ids:
        from ingestion.index_builder import build_index_for_project
        index_result = build_index_for_project(session, project, doc_ids=new_doc_ids, rebuild=True)
    else:
        index_result = None

    touched_contract_ids: Set[int] = {
        r.contract_id for r in results
        if r.status in ("INGESTED", "UPDATED") and r.contract_id
    }
    extraction_errors = _run_extraction_for_contracts(session, project, touched_contract_ids)

    ingested = sum(1 for r in results if r.status in ("INGESTED", "UPDATED"))
    skipped = sum(1 for r in results if r.status == "SKIPPED_DUPLICATE")
    failed = sum(1 for r in results if r.status == "FAILED")
    index_note = f", indexed {index_result.indexed} (failed {index_result.failed})" if index_result else ""
    return (
        f"Stage pickup: {len(staged)} file(s) found, {ingested} ingested/updated, "
        f"{skipped} unchanged, {failed} failed{index_note}. "
        f"Extraction run for {len(touched_contract_ids)} contract(s), "
        f"{len(extraction_errors)} extraction error(s)."
    )


def _run_extraction_for_contracts(session, project: ProjectConfig, contract_ids: Set[int]) -> List[str]:
    import contract_extraction
    errors = []
    for contract_id in contract_ids:
        try:
            contract_extraction.extract_stock_fields_for_contract(session, project, contract_id)
            contract_extraction.generate_contract_overview(session, project, contract_id)
            contract_extraction.generate_recommended_actions(session, project, contract_id)
            contract_extraction.generate_classification_scorecard(session, project, contract_id)
        except Exception as e:  # noqa: BLE001 — one contract's extraction failing shouldn't block the rest
            logger.exception("EVENT=STAGE_PICKUP_EXTRACTION_FAILED contract_id=%s", contract_id)
            errors.append(f"contract_id={contract_id}: {e}")
    return errors


def _extract_text(parse_result) -> str:
    data = json.loads(parse_result) if isinstance(parse_result, str) else parse_result
    return data.get("content", "") if isinstance(data, dict) else str(data)


def _log_sync_run(session, project: ProjectConfig, results: List[PickupResult]):
    from config import DATABASE, CATALOG_SCHEMA
    ingested = sum(1 for r in results if r.status in ("INGESTED", "UPDATED"))
    skipped = sum(1 for r in results if r.status == "SKIPPED_DUPLICATE")
    failed = sum(1 for r in results if r.status == "FAILED")
    session.sql(
        f"""INSERT INTO {DATABASE}.{CATALOG_SCHEMA}.PROJECT_SYNC_LOG
            (PROJECT_ID, SOURCE_TYPE, FILES_FOUND, FILES_SYNCED, FILES_SKIPPED, FILES_FAILED)
            SELECT (SELECT PROJECT_ID FROM {DATABASE}.{CATALOG_SCHEMA}.PROJECTS
                    WHERE PROJECT_CODE = ?), 'NETWORK_DRIVE_STAGE', ?, ?, ?, ?""",
        params=[project.project_code, len(results), ingested, skipped, failed],
    ).collect()
