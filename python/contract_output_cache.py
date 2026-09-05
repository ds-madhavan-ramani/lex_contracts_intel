"""
contract_output_cache.py — persists each contract's generated Word/PDF
summary (docx_report.build_contract_docx / pdf_report.build_contract_pdf)
to a dedicated stage, so the async stage-pickup Task (see
ingestion/stage_pickup.py) produces ready-to-download files as soon as
extraction finishes for a contract, rather than every Streamlit page view
regenerating them live.

Both call sites — the Task (via stage_pickup._run_extraction_for_contracts)
and the Streamlit "run/re-run extraction" buttons (Chat.py,
3_Contract_Register.py) — call cache_contract_outputs() right after
extraction, so the cache is never more than one extraction run stale.
get_cached_output() is the read side; a cache miss (nothing written yet —
e.g. before this feature existed, or a caching attempt that failed)
returns None so callers fall back to building the file live via the same
two functions this module itself calls, exactly what every page already
did before this module existed.

LEX-specific: not part of the generic project-llm-wiki template.

UNVERIFIED: session.file.put_stream/get_stream against a stage in this
form is already used elsewhere in this codebase (ingestion/
network_drive_ingest.py, ingestion/stage_pickup.py) and is assumed to
behave the same way here. What IS new: this module's write path now also
runs inside the stage-pickup stored procedure (sql/04_stage_pickup_task.sql),
which needs 'python-docx' and 'reportlab' added to that procedure's
PACKAGES list for docx_report.py/pdf_report.py to import there. Both are
common enough that Snowflake's Anaconda channel is very likely to carry
them (reportlab in particular appears in Snowflake's own stored-procedure
PDF-generation examples), but this is unverified against the live
account. If either fails to resolve, CREATE OR REPLACE PROCEDURE fails
loudly and immediately when the notebook cell runs — not a silent partial
failure. Fix in that case: drop the cache_contract_outputs() call from
stage_pickup.py's _run_extraction_for_contracts (reverting the Task to
extraction-only, its original Phase 2 behaviour) and rely on Streamlit's
own calls to this module instead — python-docx already runs fine there
today (proven by the existing .docx download button), and reportlab would
just need adding to streamlit/pyproject.toml and requirements.txt.
"""

import io
from typing import Optional

import contract_linking
import docx_report
import pdf_report
from config import ProjectConfig
from utils.logging_utils import get_logger

logger = get_logger(__name__)

OUTPUT_STAGE_NAME = "CONTRACT_OUTPUT_STAGE"

_BUILDERS = {
    "docx": (docx_report.build_contract_docx, "Contract_Workspace_Summary.docx"),
    "pdf": (pdf_report.build_contract_pdf, "Contract_Workspace_Summary.pdf"),
}


def output_stage(project: ProjectConfig) -> str:
    return f"{project.qualified_schema}.{OUTPUT_STAGE_NAME}"


def _stage_path(project: ProjectConfig, cw_number: str, filename: str) -> str:
    return f"@{output_stage(project)}/{cw_number}/{filename}"


def cache_contract_outputs(session, project: ProjectConfig, contract_id: int) -> None:
    """(Re)builds and stores both formats for one contract. Called after
    every extraction run — Task-driven or manual — so the cache always
    reflects the contract's current extracted fields. Left to raise on
    failure rather than swallowing it: both call sites already isolate
    failures per contract (stage_pickup.py's try/except loop; a
    Streamlit button's own error display), so a second layer of silent
    handling here would only hide a caching problem from both."""
    contract = contract_linking.get_contract(session, project, contract_id)
    if not contract:
        return
    cw_number = contract["CW_NUMBER"]

    for fmt, (builder, filename) in _BUILDERS.items():
        file_bytes = builder(session, project, contract_id)
        session.file.put_stream(
            io.BytesIO(file_bytes), _stage_path(project, cw_number, filename),
            auto_compress=False, overwrite=True,
        )
    logger.info("EVENT=CONTRACT_OUTPUTS_CACHED contract_id=%s cw_number=%s", contract_id, cw_number)


def get_cached_output(session, project: ProjectConfig, contract_id: int, fmt: str) -> Optional[bytes]:
    """Cached Word/PDF bytes for one contract, or None if nothing has been
    cached yet — the caller's cue to fall back to building it live."""
    contract = contract_linking.get_contract(session, project, contract_id)
    if not contract:
        return None
    _, filename = _BUILDERS[fmt]
    try:
        return session.file.get_stream(_stage_path(project, contract["CW_NUMBER"], filename)).read()
    except Exception:  # noqa: BLE001 — a missing/unreadable cached file just means "not cached yet"
        return None


def get_or_build_output(session, project: ProjectConfig, contract_id: int, fmt: str) -> bytes:
    """What every download button actually calls: serve the cached file if
    the async pickup Task (or a prior manual extraction) already produced
    one, otherwise build it live on the spot — the same fallback every
    download button used unconditionally before this cache existed, so a
    contract extracted before this feature shipped (or whose caching
    attempt failed) is never left without a download."""
    cached = get_cached_output(session, project, contract_id, fmt)
    if cached is not None:
        return cached
    builder, _ = _BUILDERS[fmt]
    return builder(session, project, contract_id)
