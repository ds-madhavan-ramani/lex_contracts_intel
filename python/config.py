"""
config.py — infra constants + per-project config loader.

Forked from the project-llm-wiki template, which also runs other projects
in production on this account. A project's settings are a
row in MEDSOCMS.APP_CATALOG.PROJECTS, fetched at runtime — only true infra
constants (shared across every project on this account) stay as
module-level constants here.

Difference from the project-llm-wiki template this was forked from: a project's
actual data can now live in its own DATABASE (ProjectConfig.data_database),
not just its own schema inside the shared MEDSOCMS database — LEX uses
this to get MEDSCOMA as a fully isolated database. The shared catalog
(APP_CATALOG.PROJECTS itself) always lives in MEDSOCMS regardless.
"""

from dataclasses import dataclass
from typing import Optional

# ---------------------------------------------------------------------------
# Infra constants — these are truly account-wide, not per-project. Warehouse
# and compute pool are NOT here — they're per-project columns on PROJECTS
# (QUERY_WAREHOUSE, COMPUTE_POOL), since different projects may want
# different compute. MTMWH02 below is only the *fallback default* a new
# project gets if none is specified at creation time. DATABASE/CATALOG_SCHEMA
# are where the shared catalog itself lives — NOT necessarily where a given
# project's own data lives; see ProjectConfig.data_database for that.
# ---------------------------------------------------------------------------
WAREHOUSE_NAME = "MTMWH02"  # default only — see ProjectConfig.query_warehouse
DATABASE = "MEDSOCMS"       # home of the shared catalog (APP_CATALOG), always
ROLE = "ADVANCEDANALYTICS"
CATALOG_SCHEMA = "APP_CATALOG"

# Graph API app registration — tenant-level, shared by default across every
# project's SharePoint/network-drive ingestion. Tenant ID / Client ID are
# not secret (Microsoft treats both as public identifiers, visible on the
# app registration's own Overview page), so they're plain constants here,
# unlike the client secret — which is fetched from the Snowflake SECRET
# object referenced in sql/test_graph_connectivity.sql and never stored in
# this repo. A project can override all three via its own
# GRAPH_TENANT_ID/GRAPH_CLIENT_ID/GRAPH_SECRET_NAME columns on PROJECTS
# (see ProjectConfig properties below) for a dedicated, least-privilege
# registration instead of this shared default.
GRAPH_TENANT_ID = "23cc5cff-1cb6-4a63-9c82-97d2a2721787"
GRAPH_CLIENT_ID = "8bc3d2b4-594a-4fd1-a9c8-bf6ef8db1caa"
GRAPH_SECRET_NAME = f"{DATABASE}.{CATALOG_SCHEMA}.GRAPH_API_SECRET"

# Model fallback if a project row doesn't specify one
FREE_MODEL = "llama3.1-70b"

MIN_PARSED_TEXT_CHARS = 100


@dataclass
class ProjectConfig:
    """One row of MEDSOCMS.APP_CATALOG.PROJECTS, typed."""
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
    sharepoint_site_url: Optional[str]
    sharepoint_default_folder: Optional[str]
    graph_tenant_id: Optional[str]
    graph_client_id: Optional[str]
    graph_secret_name: Optional[str]
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

    @property
    def resolved_graph_tenant_id(self) -> str:
        """This project's Graph tenant ID, or the shared tenant-level
        default if it hasn't been given its own dedicated registration."""
        return self.graph_tenant_id or GRAPH_TENANT_ID

    @property
    def resolved_graph_client_id(self) -> str:
        return self.graph_client_id or GRAPH_CLIENT_ID

    @property
    def resolved_graph_secret_name(self) -> str:
        """Fully-qualified secret object name. Note this only affects which
        SECRET object the deploy step binds under the Streamlit app's fixed
        local alias 'graph_secret' — application code always reads
        st.secrets['graph_secret'] regardless (see utils/graph_client.py),
        so no code path needs to know which underlying secret that alias
        actually points to."""
        return self.graph_secret_name or GRAPH_SECRET_NAME


def load_project(session, project_code: str) -> ProjectConfig:
    """Fetch a project's config row. Raises ValueError if not found/archived."""
    rows = session.sql(
        f"""SELECT PROJECT_ID, PROJECT_CODE, PROJECT_NAME, DESCRIPTION,
                   DATA_DATABASE, DATA_SCHEMA, STAGE_NAME,
                   STREAMLIT_APP_NAME, STREAMLIT_STAGE_NAME,
                   QUERY_WAREHOUSE, COMPUTE_POOL,
                   SHAREPOINT_SITE_URL, SHAREPOINT_DEFAULT_FOLDER,
                   GRAPH_TENANT_ID, GRAPH_CLIENT_ID, GRAPH_SECRET_NAME,
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
        sharepoint_site_url=r["SHAREPOINT_SITE_URL"],
        sharepoint_default_folder=r["SHAREPOINT_DEFAULT_FOLDER"],
        graph_tenant_id=r["GRAPH_TENANT_ID"],
        graph_client_id=r["GRAPH_CLIENT_ID"],
        graph_secret_name=r["GRAPH_SECRET_NAME"],
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
