"""
snowflake_session.py — single place that produces a Snowpark Session.

Unchanged from the project-llm-wiki template (ds-madhavan-ramani/org_mm_chat).
Tries, in order:
  1. get_active_session() — works in Snowflake Notebooks and warehouse-runtime
     Streamlit apps, but is NOT supported in container runtime.
  2. st.connection("snowflake") — the required pattern for container-runtime
     Streamlit apps (Snowflake's official migration guidance: "Replace
     get_active_session() with st.connection('snowflake')"). LEX runs on
     container runtime by default, so this is the path it actually takes.
  3. External connection (local dev, CLI scripts) via env vars.
"""

import os
import logging

from config import WAREHOUSE_NAME, DATABASE, ROLE

logger = logging.getLogger(__name__)


def get_session():
    # 1. Snowflake Notebook / warehouse-runtime Streamlit
    try:
        from snowflake.snowpark.context import get_active_session
        return get_active_session()
    except Exception:
        pass

    # 2. Container-runtime Streamlit — required pattern per Snowflake's
    #    warehouse-to-container migration docs.
    try:
        import streamlit as st
        conn = st.connection("snowflake")
        return conn.session()
    except Exception:
        pass

    # 3. External connection (local dev, CLI scripts)
    from snowflake.snowpark import Session

    connection_parameters = {
        "account": os.environ["SNOWFLAKE_ACCOUNT"],
        "user": os.environ["SNOWFLAKE_USER"],
        "role": ROLE,
        "warehouse": WAREHOUSE_NAME,
        "database": DATABASE,
    }

    if os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH"):
        with open(os.environ["SNOWFLAKE_PRIVATE_KEY_PATH"], "rb") as f:
            connection_parameters["private_key"] = f.read()
    elif os.environ.get("SNOWFLAKE_PASSWORD"):
        connection_parameters["password"] = os.environ["SNOWFLAKE_PASSWORD"]
    else:
        raise RuntimeError(
            "No Snowflake auth found. Set SNOWFLAKE_PRIVATE_KEY_PATH or "
            "SNOWFLAKE_PASSWORD, or run inside Snowflake Notebook/Streamlit."
        )

    logger.info("Connecting to Snowflake as external session (account=%s)",
                connection_parameters["account"])
    return Session.builder.configs(connection_parameters).create()
