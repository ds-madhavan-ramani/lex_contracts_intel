-- ============================================================================
-- LEX : test_graph_connectivity.sql
-- Run once after setup to confirm the External Access Integration works.
--
-- LEX does not reuse a shared, tenant-level Graph app registration the way
-- the project-llm-wiki template normally defaults to — it has its own
-- dedicated app registration, secret, network rule, and external access
-- integration, all scoped to just the contracts library and none of it
-- shared with any other project on this account. GRAPH_TENANT_ID/
-- GRAPH_CLIENT_ID live as plain values on LEX's PROJECTS row in
-- python/config.py's terms; the client secret lives only in the SECRET
-- object created below and is bound to the Streamlit app at deploy time
-- under the fixed local alias 'graph_secret' (see
-- pipeline/00_provision_project.ipynb's deploy cell).
-- ============================================================================

USE ROLE ADVANCEDANALYTICS;
USE WAREHOUSE MTMWH02;
USE DATABASE MEDSCOMA;

-- Expect these to already exist once the one-time setup below has been run.
-- All three are LEX's own — nothing here is shared with any other project:
--   SECRET             MEDSCOMA.APP_CATALOG.LEX_GRAPH_API_SECRET
--   NETWORK RULE        MEDSCOMA.APP_CATALOG.LEX_GRAPH_API_NETWORK_RULE
--   EXTERNAL ACCESS      LEX_GRAPH_API_ACCESS_INTEGRATION

SHOW SECRETS LIKE 'LEX_GRAPH_API_SECRET' IN SCHEMA MEDSCOMA.APP_CATALOG;
SHOW NETWORK RULES LIKE 'LEX_GRAPH_API_NETWORK_RULE' IN SCHEMA MEDSCOMA.APP_CATALOG;
SHOW EXTERNAL ACCESS INTEGRATIONS LIKE 'LEX_GRAPH_API_ACCESS_INTEGRATION';

-- ----------------------------------------------------------------------------
-- One-time setup: run once, before LEX's Data Sources page can reach
-- SharePoint. Needs an Azure AD app registration with Sites.Selected
-- permission granted on the contracts library (create that in Azure AD
-- first — this script only creates the Snowflake-side objects). Typically
-- SYSADMIN/ACCOUNTADMIN to create a SECRET/NETWORK RULE/EXTERNAL ACCESS
-- INTEGRATION — the same gotcha this template's own README documents for
-- compute pools.
-- ----------------------------------------------------------------------------

CREATE SECRET IF NOT EXISTS MEDSCOMA.APP_CATALOG.LEX_GRAPH_API_SECRET
  TYPE = GENERIC_STRING
  SECRET_STRING = '<lex_client_secret>';

CREATE NETWORK RULE IF NOT EXISTS MEDSCOMA.APP_CATALOG.LEX_GRAPH_API_NETWORK_RULE
  MODE = EGRESS
  TYPE = HOST_PORT
  VALUE_LIST = ('login.microsoftonline.com', 'graph.microsoft.com');

CREATE EXTERNAL ACCESS INTEGRATION IF NOT EXISTS LEX_GRAPH_API_ACCESS_INTEGRATION
  ALLOWED_NETWORK_RULES = (MEDSCOMA.APP_CATALOG.LEX_GRAPH_API_NETWORK_RULE)
  ALLOWED_AUTHENTICATION_SECRETS = (MEDSCOMA.APP_CATALOG.LEX_GRAPH_API_SECRET)
  ENABLED = TRUE;

GRANT USAGE ON SECRET MEDSCOMA.APP_CATALOG.LEX_GRAPH_API_SECRET TO ROLE ADVANCEDANALYTICS;

-- Then set LEX's own tenant/client IDs and the secret's fully-qualified
-- name on its PROJECTS row — config.py's resolved_graph_* properties raise
-- a clear error until all three are populated, since there's no shared
-- fallback to silently fall through to:
--
--   UPDATE MEDSCOMA.APP_CATALOG.PROJECTS
--     SET GRAPH_TENANT_ID = '<lex_tenant_id>',
--         GRAPH_CLIENT_ID = '<lex_client_id>',
--         GRAPH_SECRET_NAME = 'MEDSCOMA.APP_CATALOG.LEX_GRAPH_API_SECRET'
--   WHERE PROJECT_CODE = 'LEX';
--
-- and the deploy notebook cell's SECRETS clause will bind that secret
-- under the app's fixed 'graph_secret' alias — see
-- pipeline/00_provision_project.ipynb.
