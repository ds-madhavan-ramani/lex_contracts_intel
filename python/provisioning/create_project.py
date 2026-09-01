"""
provisioning/create_project.py — thin CLI wrapper around the CREATE_PROJECT
stored procedure (sql/00_setup_catalog.sql). Used by the setup notebook.

Forked from the project-llm-wiki template;
adds --data-database (default MEDSCOMA — LEX's own dedicated database,
never the shared MEDSOCMS this template otherwise defaults to) since a
project's data can now live in its own database, not just its own schema.
Also replaces --sharepoint-site/--sharepoint-folder with
--network-drive-host/--network-drive-share — LEX's contracts library was
confirmed to be a genuine on-prem network drive (SMB), not SharePoint.

    python provisioning/create_project.py \\
        --code LEX \\
        --name "LEX - Legal EXtraction & Contract Intelligence" \\
        --description "Contract Q&A and stock-field extraction for signed contracts" \\
        --network-drive-host fileserver.mtm.local \\
        --network-drive-share Contracts \\
        --query-warehouse MTMWH02 \\
        --compute-pool STREAMLIT_COMPUTE_POOL_CONTRACT_MGMT \\
        --data-database MEDSCOMA
"""

import argparse
import sys

sys.path.insert(0, "..")
from snowflake_session import get_session  # noqa: E402


def create_project(code: str, name: str, description: str = "",
                   network_drive_host: str = "", network_drive_share: str = "",
                   created_by: str = "", query_warehouse: str = "",
                   compute_pool: str = "", data_database: str = "") -> str:
    session = get_session()
    result = session.sql(
        "CALL CREATE_PROJECT(?, ?, ?, ?, ?, ?, ?, ?, ?)",
        params=[code, name, description, network_drive_host, network_drive_share,
                created_by, query_warehouse, compute_pool, data_database],
    ).collect()
    return result[0][0]


def main():
    parser = argparse.ArgumentParser(description="Provision a new LLM Wiki project")
    parser.add_argument("--code", required=True, help="Short code, e.g. LEX")
    parser.add_argument("--name", required=True, help="Display name")
    parser.add_argument("--description", default="")
    parser.add_argument("--network-drive-host", default="", help="e.g. fileserver.mtm.local")
    parser.add_argument("--network-drive-share", default="", help="e.g. Contracts")
    parser.add_argument("--created-by", default="")
    parser.add_argument("--query-warehouse", default="", help="Default: MTMWH02")
    parser.add_argument("--compute-pool", default="", help="Blank = warehouse runtime")
    parser.add_argument("--data-database", default="", help="Default: MEDSCOMA")
    args = parser.parse_args()

    message = create_project(
        code=args.code, name=args.name, description=args.description,
        network_drive_host=args.network_drive_host, network_drive_share=args.network_drive_share,
        created_by=args.created_by, query_warehouse=args.query_warehouse,
        compute_pool=args.compute_pool, data_database=args.data_database,
    )
    print(message)


if __name__ == "__main__":
    main()
