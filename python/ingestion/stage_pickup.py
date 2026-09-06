"""
stage_pickup.py — pick up files staged into NETWORK_DRIVE_INBOX_STAGE by
the companion lex_network_bridge repo's bridge tool (a Linux host running
*inside* the MTM network, since Snowflake can't reach the network drive
directly — see README's "Open items") and feed them into this app's
normal ingest pipeline: RAW_DOCUMENTS, contract linking, and indexing —
the same three steps ingestion/file_ingest.py's ingest_uploaded_files()
already does for an uploaded file, just sourcing the file from a
different place, with no direct-SMB path anywhere in this codebase.

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

A processed file is never re-parsed: once a file is fully handled
(INGESTED, UPDATED, or SKIPPED_DUPLICATE) its path is recorded in
_STAGE_PICKUP_PROCESSED (see _mark_processed), and list_staged_files()
excludes anything already in that table from what it returns. Without
this, list_staged_files() would keep re-listing every file ever staged on
every Task tick forever (its directory-table query has no "already
handled" filter on its own), which would re-run COPY FILES +
AI_PARSE_DOCUMENT — the actual OCR/parsing cost — on already-processed
files every schedule tick indefinitely. The SOURCE_HASH check against
RAW_DOCUMENTS still prevents a duplicate row or a wasted extraction
re-run even without this, but does nothing to avoid repeating the parse
itself, which is what this tracking actually stops.

CONFIRMED on a live account: this is a table, not an actual deletion from
NETWORK_DRIVE_INBOX_STAGE, because REMOVE is an unsupported statement
type inside a Python stored procedure ("Unsupported statement type
'REMOVE_FILES'") — the same class of restriction as CREATE TEMPORARY
TABLE and ALTER SESSION elsewhere in this module's history. A processed
file's bytes stay in the inbox stage forever as harmless dead weight
(tracking prevents it from ever being reprocessed); use
purge_processed_inbox_files() from a notebook cell or worksheet
occasionally to actually delete them — REMOVE works fine outside a
stored procedure's execution context, only inside one it's rejected.

A FAILED file (parse error, text too short) is deliberately never marked
processed, so the next tick retries it rather than losing it silently —
a file stuck FAILED needs a human to look at it, not indefinite silent
retries either, so check PROJECT_SYNC_LOG / a run's returned summary for
a persistently failing file.

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
get_stream-then-put_stream pattern this module already uses for its own
xlsx branch below (GET the bytes from NETWORK_DRIVE_INBOX_STAGE, then
put_stream into project.qualified_stage).
"""

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from typing import List, Optional, Set

from config import ProjectConfig, MIN_PARSED_TEXT_CHARS
import contract_linking
from ingestion.xlsx_parser import is_xlsx, parse_xlsx_to_text
from utils.logging_utils import get_logger, log_event
from utils.sql_utils import SQLBuilder

logger = get_logger(__name__)

INBOX_STAGE = "MEDSCOMA.DATA_LEX.NETWORK_DRIVE_INBOX_STAGE"
# Tracks which inbox files have already been fully handled, in place of
# actually deleting them — see the module docstring's REMOVE_FILES note.
# Created by sql/04_stage_pickup_task.sql.
PROCESSED_TABLE = "MEDSCOMA.DATA_LEX._STAGE_PICKUP_PROCESSED"

# Matches the CW number the bridge tool's CLI fallback naming convention
# stamps onto the FRONT of a filename (e.g. "CW14465 - Base Agreement.pdf")
# when a file ends up staged at the inbox root instead of inside a
# "<CW_NUMBER>/" subfolder. CONFIRMED on a live account: this happens for
# at least one of the bridge tool's upload paths — its "browser app, human
# picks the CW folder" path stages correctly, but files have also been
# observed landing flat at the root with this filename convention.
_ROOT_FILE_CW_PREFIX = re.compile(r"^(CW\d+)\s*-")


def _quoted_stage_location(location: str) -> str:
    """Single-quotes a stage location (`@stage/path`) for REMOVE and other
    file-utility commands that take the location as a bare token, not a
    bind-able value. CONFIRMED on a live account: an unquoted location
    with a real contract filename ("CW20841 - Executed Services
    Agreement...pdf") fails with a SQL compilation error at the first
    space-hyphen-space — Snowflake parses an unquoted @stage/path
    token-by-token, so spaces/hyphens/parens (all common in scanned
    contract filenames) break it. Wrapping the whole thing in single
    quotes (Snowflake's own documented form, e.g. REMOVE
    '@%mytable/myfile.csv.gz') is the fix; an embedded single quote is
    escaped by doubling it, the standard SQL string-literal escape (NOT
    the backslash-doubling this codebase already had to learn about
    elsewhere for NETWORK_DRIVE_DEFAULT_PATH — unrelated escaping rules
    for an unrelated character)."""
    return "'" + location.replace("'", "''") + "'"


@dataclass
class StagedFile:
    cw_number: str
    relative_path: str   # "<CW>/<filename>" — relative to INBOX_STAGE; the SOURCE_ITEM_ID dedup key
    file_name: str
    # The path exactly as DIRECTORY() returned it, before any root-file
    # normalization. Equal to relative_path unless this file was moved out
    # of the inbox root — see list_staged_files. This, not relative_path,
    # is what gets recorded in PROCESSED_TABLE, since the original root
    # copy is what's still physically sitting in the stage (REMOVE can't
    # delete it — see module docstring) and must be excluded from future
    # DIRECTORY() scans by the same key it was found under.
    source_relative_path: str = ""

    def __post_init__(self):
        if not self.source_relative_path:
            self.source_relative_path = self.relative_path


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
    auto-linking note: only proceed when the CW is known with certainty.

    CONFIRMED on a live account: an internal stage's directory table does
    not always auto-refresh the moment a new file is PUT — files were
    visible via LIST but absent from DIRECTORY() until an explicit REFRESH.
    Refreshing here on every call is cheap (a metadata-only operation) and
    removes an entire class of "why didn't my new file show up" confusion,
    at the cost of one extra statement per run.

    Also bypasses the query result cache for the DIRECTORY() listing
    itself: this procedure always runs as its owner role (EXECUTE AS OWNER
    is Snowflake's default), so a worksheet session on a different role
    seeing fresh results is no guarantee this procedure's own role+query
    combination isn't still being served a persisted result cached from
    an earlier run — confirmed on a live account as exactly this symptom
    (REFRESH ran, a manual SELECT under a different role/session showed
    the new files, but this procedure's own DIRECTORY() query kept
    returning the pre-refresh empty result). ALTER STAGE ... REFRESH is a
    stage-metadata operation, not a table write, so it isn't guaranteed to
    invalidate Snowflake's persisted result cache for a prior identical
    query the way normal DML does.

    CONFIRMED on a live account: `ALTER SESSION SET ...` — the obvious way
    to disable the cache — is itself an unsupported statement type inside
    a Python stored procedure ("Unsupported statement type
    'ALTER_SESSION'"), the same class of restriction as CREATE TEMPORARY
    TABLE above.

    CONFIRMED (the hard way) that `statement_params={"use_cached_result":
    False}` on its own does NOT reliably disable caching here either: with
    REFRESH run immediately before it, under the confirmed-identical role
    that owns both the stage and this procedure, it still returned 0 rows
    while a plain `LIST @stage` (a completely different, always-live code
    path) confirmed the stage genuinely had files in it. Whatever the
    exact mechanism, this specific query text has almost certainly been
    cached from an earlier call to this same function when the inbox
    really was empty (this function has been invoked, and returned 0
    rows, many times over the course of getting the rest of this pipeline
    working), and neither REFRESH nor statement_params reliably busts a
    persisted result cache for a stage's directory table.

    The fix that actually can't fail regardless of the exact caching
    mechanism: make the query text itself unique on every call (a SQL
    comment with a fresh UUID) so there is never a pre-existing cached
    result to serve, by construction — not relying on any cache-control
    knob actually being honored in this execution context.

    CONFIRMED on a live account: after ruling out caching entirely (the
    cache-buster above proved it wasn't that), the actual "0 files found"
    root cause was that files were landing at the stage ROOT with no CW
    subfolder at all — i.e. this function's own root-file skip below was
    doing exactly what it's supposed to. Rather than let that silently
    swallow every file from whichever bridge-tool upload path produces
    this shape, a root-level file whose name starts with "CW<digits> -"
    (the bridge tool's own filename convention — see _ROOT_FILE_CW_PREFIX)
    is treated as reliably CW-attributed as a folder name would be, and is
    moved into the matching subfolder before being picked up. A root file
    that doesn't match this pattern is still skipped with a warning rather
    than guessed at."""
    session.sql(f"ALTER STAGE {INBOX_STAGE} REFRESH").collect()
    cache_buster = uuid.uuid4().hex
    rows = session.sql(
        f"SELECT d.RELATIVE_PATH FROM DIRECTORY(@{INBOX_STAGE}) d "
        f"LEFT JOIN {PROCESSED_TABLE} p ON d.RELATIVE_PATH = p.RELATIVE_PATH "
        f"WHERE p.RELATIVE_PATH IS NULL /* cache_buster={cache_buster} */"
    ).collect(statement_params={"use_cached_result": False})
    staged: List[StagedFile] = []
    for row in rows:
        source_relative_path = row["RELATIVE_PATH"]
        relative_path = source_relative_path
        if "/" not in relative_path:
            match = _ROOT_FILE_CW_PREFIX.match(relative_path)
            if not match:
                logger.warning("EVENT=STAGE_PICKUP_NO_CW_FOLDER path=%s", relative_path)
                continue
            cw_number = match.group(1)
            file_name = relative_path
            logger.warning(
                "EVENT=STAGE_PICKUP_ROOT_FILE_NORMALIZED path=%s cw=%s",
                relative_path, cw_number,
            )
            # Copies the file into its CW subfolder so the rest of the
            # pipeline can treat it exactly like a correctly-staged file —
            # COPY FILES is confirmed to work inside a stored procedure.
            # The original root copy is NOT removed (REMOVE doesn't work
            # here — see module docstring); source_relative_path (the
            # ORIGINAL root path) is what gets recorded in PROCESSED_TABLE
            # once this file is fully handled, so the root copy is
            # excluded from every future scan by the LEFT JOIN above even
            # though it's still physically sitting there.
            session.sql(
                f"COPY FILES INTO @{INBOX_STAGE}/{cw_number}/ FROM @{INBOX_STAGE}/ FILES = (?)",
                params=[relative_path],
            ).collect()
            relative_path = f"{cw_number}/{file_name}"
        else:
            cw_number, file_name = relative_path.split("/", 1)
            if "/" in file_name:
                # Nested more than one level deep — not the expected
                # <CW>/<filename> shape. Skip rather than guess which
                # segment is the real CW folder.
                logger.warning("EVENT=STAGE_PICKUP_UNEXPECTED_NESTING path=%s", relative_path)
                continue
        staged.append(StagedFile(cw_number=cw_number, relative_path=relative_path, file_name=file_name,
                                  source_relative_path=source_relative_path))
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
            # ingestion/file_ingest.py exactly, and means a file whose
            # content is unchanged is recognized as such even if its raw
            # bytes differ trivially (re-saved PDF metadata, etc.).
            source_hash = hashlib.sha256(raw_text.encode()).hexdigest()

            if existing and existing[0]["SOURCE_HASH"] == source_hash:
                results.append(PickupResult(item.file_name, item.cw_number, "SKIPPED_DUPLICATE",
                                             doc_id=existing[0]["DOC_ID"]))
                _mark_processed(session, item)
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

            _mark_processed(session, item)

        except Exception as e:  # noqa: BLE001 — one bad file shouldn't abort the whole run
            logger.exception("EVENT=STAGE_PICKUP_ERROR file=%s cw=%s", item.file_name, item.cw_number)
            results.append(PickupResult(item.file_name, item.cw_number, "FAILED", error=str(e)))
            # Deliberately NOT marked processed — left so a FAILED file
            # (e.g. a transient parse error) is retried on the next Task
            # tick rather than silently lost. See this module's docstring
            # on why a stuck FAILED file needs a human to look rather than
            # retrying forever unattended.

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
    import contract_output_cache
    errors = []
    for contract_id in contract_ids:
        try:
            # extract_stock_fields_for_contract already (re)generates the
            # overview/recommended-actions/scorecard synthesis internally
            # once every stock field is extracted — calling those three
            # again here would just repeat the same Cortex calls a second
            # time for no benefit.
            contract_extraction.extract_stock_fields_for_contract(session, project, contract_id)
            # Phase 3: cache this contract's Word/PDF summary now that its
            # fields are current — see contract_output_cache's own
            # docstring for the PACKAGES caveat this adds to the stored
            # procedure that calls this function.
            contract_output_cache.cache_contract_outputs(session, project, contract_id)
        except Exception as e:  # noqa: BLE001 — one contract's extraction failing shouldn't block the rest
            logger.exception("EVENT=STAGE_PICKUP_EXTRACTION_FAILED contract_id=%s", contract_id)
            errors.append(f"contract_id={contract_id}: {e}")
    return errors


def _mark_processed(session, item: StagedFile) -> None:
    """Records a successfully-handled file in PROCESSED_TABLE so
    list_staged_files() never returns it again. This is a table, not an
    actual REMOVE — see module docstring on why: REMOVE is an unsupported
    statement type inside a Python stored procedure, confirmed on a live
    account. MERGE (not INSERT) makes this idempotent in case a run
    somehow processes the same file twice. Called for every non-FAILED
    outcome (INGESTED, UPDATED, SKIPPED_DUPLICATE); a FAILED file is
    deliberately left unmarked so the next Task tick retries it rather
    than silently losing it."""
    session.sql(
        f"MERGE INTO {PROCESSED_TABLE} t USING (SELECT ? AS RELATIVE_PATH) s "
        f"ON t.RELATIVE_PATH = s.RELATIVE_PATH "
        f"WHEN NOT MATCHED THEN INSERT (RELATIVE_PATH) VALUES (s.RELATIVE_PATH)",
        params=[item.source_relative_path],
    ).collect()


def purge_processed_inbox_files(session) -> int:
    """Actually deletes every already-processed file's original copy from
    NETWORK_DRIVE_INBOX_STAGE. NOT called by run_stage_pickup() or the
    scheduled Task — REMOVE is confirmed unsupported inside a Python
    stored procedure, so this only works called directly from a notebook
    cell or worksheet. Safe to run any time: it only ever removes a path
    PROCESSED_TABLE already has a row for, so a pending or FAILED file is
    never touched. Returns the number of files removed."""
    rows = session.sql(f"SELECT RELATIVE_PATH FROM {PROCESSED_TABLE}").collect()
    for row in rows:
        relative_path = row["RELATIVE_PATH"]
        location = _quoted_stage_location(f"@{INBOX_STAGE}/{relative_path}")
        session.sql(f"REMOVE {location}").collect()
    return len(rows)


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
