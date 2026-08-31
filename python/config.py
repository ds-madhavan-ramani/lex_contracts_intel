"""
config.py — infra constants + LEX's config loader.

Forked from the project-llm-wiki template, which centralizes its catalog
and Graph API registration in a shared MEDSOCMS database used by every
project on the account. LEX does not follow that part of the template:
it is fully self-contained in its own MEDSCOMA database — catalog
(APP_CATALOG.PROJECTS), data (DATA_LEX), Streamlit app/stage, and its own
dedicated Graph API secret/network rule all live there. Nothing LEX reads
or writes lives in MEDSOCMS, and it holds no reference to any other
project's resources.
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
        """LEX's own Graph API tenant ID, set on the PROJECTS row by the
        provisioning notebook's Graph API registration cell. There is no
        shared/tenant-level fallback — LEX does not reuse any other
        project's app registration — so this raises clearly instead of
        silently falling through to an undefined value."""
        if not self.graph_tenant_id:
            raise ValueError(
                "GRAPH_TENANT_ID is not set on the PROJECTS row. Run the Graph "
                "API registration cell in pipeline/00_provision_project.ipynb "
                "first — LEX has no shared registration to fall back to."
            )
        return self.graph_tenant_id

    @property
    def resolved_graph_client_id(self) -> str:
        if not self.graph_client_id:
            raise ValueError(
                "GRAPH_CLIENT_ID is not set on the PROJECTS row. Run the Graph "
                "API registration cell in pipeline/00_provision_project.ipynb "
                "first — LEX has no shared registration to fall back to."
            )
        return self.graph_client_id

    @property
    def resolved_graph_secret_name(self) -> str:
        """Fully-qualified secret object name — always LEX's own, e.g.
        MEDSCOMA.APP_CATALOG.LEX_GRAPH_API_SECRET (see
        sql/test_graph_connectivity.sql). Note this only affects which
        SECRET object the deploy step binds under the Streamlit app's fixed
        local alias 'graph_secret' — application code always reads
        st.secrets['graph_secret'] regardless (see utils/graph_client.py),
        so no code path needs to know which underlying secret that alias
        actually points to."""
        if not self.graph_secret_name:
            raise ValueError(
                "GRAPH_SECRET_NAME is not set on the PROJECTS row. Run the Graph "
                "API registration cell in pipeline/00_provision_project.ipynb "
                "first — LEX has no shared secret to fall back to."
            )
        return self.graph_secret_name


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
