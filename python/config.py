"""
config.py — infra constants + LEX's config loader.

Forked from the project-llm-wiki template, which centralizes its catalog
in a shared MEDSOCMS database and normally ingests from SharePoint via
Microsoft Graph API. LEX follows neither part of the template: it is
fully self-contained in its own MEDSCOMA database — catalog
(APP_CATALOG.PROJECTS), data (DATA_LEX), and Streamlit app/stage all live
there. Nothing LEX reads or writes lives in MEDSOCMS, and it holds no
reference to any other project's resources.

Documents arrive via two paths: direct upload (ingestion/file_ingest.py)
and the companion lex_network_bridge repo's stage-pickup Task
(ingestion/stage_pickup.py) — LEX's contracts library is a genuine
on-prem network drive (SMB), but Snowflake's outbound network can't
resolve it directly (see README's "Open items"), so there is no
direct-SMB or Graph API ingestion path in this codebase.
"""

from dataclasses import dataclass
from typing import Optional

# ---------------------------------------------------------------------------
# Infra constants. Warehouse and compute pool are NOT here — they're
# per-project columns on PROJECTS (QUERY_WAREHOUSE, COMPUTE_POOL). MTMWH02
# below is only the *fallback default* if none is specified at creation
# time. DATABASE/CATALOG_SCHEMA are where LEX's own catalog lives —
# MEDSCOMA, the same dedicated database LEX's actual data lives in (see
# ProjectConfig.data_database) — not a database shared with anything else.
# ---------------------------------------------------------------------------
WAREHOUSE_NAME = "MTMWH02"  # default only — see ProjectConfig.query_warehouse
DATABASE = "MEDSCOMA"       # home of LEX's own catalog (APP_CATALOG), always
ROLE = "ADVANCEDANALYTICS"
CATALOG_SCHEMA = "APP_CATALOG"

# Model fallback if a project row doesn't specify one
FREE_MODEL = "llama3.1-70b"

MIN_PARSED_TEXT_CHARS = 100


@dataclass
class ProjectConfig:
    """One row of MEDSCOMA.APP_CATALOG.PROJECTS, typed."""
    project_id: int
    project_code: str
    project_name: str
    description: Optional[str]
    data_database: str
    data_schema: str
    stage_name: str
    streamlit_app_name: str
    streamlit_stage_name: str
    query_warehouse: str
    compute_pool: Optional[str]
    active_model: str
    max_document_chars: int
    max_section_chars: int
    query_cache_ttl_hours: int
    max_citations_display: int
    segmentation_profile: str
    status: str
    segmentation_granularity: str
    enable_reranking: bool
    enable_vector_search: bool
    max_candidate_docs: int

    @property
    def qualified_schema(self) -> str:
        return f"{self.data_database}.{self.data_schema}"

    @property
    def qualified_stage(self) -> str:
        return f"{self.qualified_schema}.{self.stage_name}"

    @property
    def qualified_streamlit_app(self) -> str:
        return f"{DATABASE}.{CATALOG_SCHEMA}.{self.streamlit_app_name}"

    @property
    def qualified_streamlit_stage(self) -> str:
        return f"{DATABASE}.{CATALOG_SCHEMA}.{self.streamlit_stage_name}"

    @property
    def is_container_runtime(self) -> bool:
        return bool(self.compute_pool)

    @property
    def clamped_max_candidate_docs(self) -> int:
        """MAX_CANDIDATE_DOCS is a config number, not DB-enforced (Snowflake
        accepts CHECK constraint syntax but never enforces it) — clamp here."""
        return max(5, min(10, self.max_candidate_docs))


def load_project(session, project_code: str) -> ProjectConfig:
    """Fetch a project's config row. Raises ValueError if not found/archived."""
    rows = session.sql(
        f"""SELECT PROJECT_ID, PROJECT_CODE, PROJECT_NAME, DESCRIPTION,
                   DATA_DATABASE, DATA_SCHEMA, STAGE_NAME,
                   STREAMLIT_APP_NAME, STREAMLIT_STAGE_NAME,
                   QUERY_WAREHOUSE, COMPUTE_POOL,
                   ACTIVE_MODEL, MAX_DOCUMENT_CHARS, MAX_SECTION_CHARS,
                   QUERY_CACHE_TTL_HOURS, MAX_CITATIONS_DISPLAY,
                   SEGMENTATION_PROFILE, STATUS,
                   SEGMENTATION_GRANULARITY, ENABLE_RERANKING,
                   ENABLE_VECTOR_SEARCH, MAX_CANDIDATE_DOCS
            FROM {DATABASE}.{CATALOG_SCHEMA}.PROJECTS
            WHERE PROJECT_CODE = ?""",
        params=[project_code.strip().upper()],
    ).collect()

    if not rows:
        raise ValueError(f"No project found with code '{project_code}'")

    r = rows[0]
    return ProjectConfig(
        project_id=r["PROJECT_ID"],
        project_code=r["PROJECT_CODE"],
        project_name=r["PROJECT_NAME"],
        description=r["DESCRIPTION"],
        data_database=r["DATA_DATABASE"],
        data_schema=r["DATA_SCHEMA"],
        stage_name=r["STAGE_NAME"],
        streamlit_app_name=r["STREAMLIT_APP_NAME"],
        streamlit_stage_name=r["STREAMLIT_STAGE_NAME"],
        query_warehouse=r["QUERY_WAREHOUSE"],
        compute_pool=r["COMPUTE_POOL"],
        active_model=r["ACTIVE_MODEL"],
        max_document_chars=r["MAX_DOCUMENT_CHARS"],
        max_section_chars=r["MAX_SECTION_CHARS"],
        query_cache_ttl_hours=r["QUERY_CACHE_TTL_HOURS"],
        max_citations_display=r["MAX_CITATIONS_DISPLAY"],
        segmentation_profile=r["SEGMENTATION_PROFILE"],
        status=r["STATUS"],
        segmentation_granularity=r["SEGMENTATION_GRANULARITY"],
        enable_reranking=bool(r["ENABLE_RERANKING"]),
        enable_vector_search=bool(r["ENABLE_VECTOR_SEARCH"]),
        max_candidate_docs=r["MAX_CANDIDATE_DOCS"],
    )


def list_active_projects(session):
    """Used by the Streamlit project selector."""
    return session.sql(
        f"""SELECT PROJECT_CODE, PROJECT_NAME
            FROM {DATABASE}.{CATALOG_SCHEMA}.PROJECTS
            WHERE STATUS = 'ACTIVE'
            ORDER BY PROJECT_NAME"""
    ).collect()
