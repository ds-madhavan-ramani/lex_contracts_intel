-- ============================================================================
-- LEX : test_graph_connectivity.sql
-- Run once after setup to confirm the External Access Integration works.
--
-- Like every other project on this shared project-llm-wiki template, LEX
-- defaults to reusing one tenant-level Graph app registration too —
-- GRAPH_TENANT_ID/GRAPH_CLIENT_ID in
-- python/config.py, secret MEDSOCMS.APP_CATALOG.GRAPH_API_SECRET — unless
-- and until a dedicated app registration is provisioned for LEX (see the
-- README's security notes). If/when a dedicated registration exists, set
-- PROJECTS.GRAPH_TENANT_ID / GRAPH_CLIENT_ID / GRAPH_SECRET_NAME on LEX's
-- row and create its own SECRET object; the network rule and external
-- access integration below can still be shared (they only allow-list
-- HOSTS, not credentials — ALLOWED_AUTHENTICATION_SECRETS is a list, so a
-- second project's secret is just added to it, not a replacement).
-- ============================================================================

USE ROLE ADVANCEDANALYTICS;
USE WAREHOUSE MTMWH02;
USE DATABASE MEDSOCMS;

-- Expect these to already exist (created outside this repo — shared across
-- every project on this account, since the Azure AD app registration and
-- the Graph API hostnames are the same regardless of which project calls
-- them):
--   SECRET             MEDSOCMS.APP_CATALOG.GRAPH_API_SECRET
--   NETWORK RULE        MEDSOCMS.APP_CATALOG.GRAPH_API_NETWORK_RULE
--   EXTERNAL ACCESS      MEDSOCMS.APP_CATALOG.GRAPH_API_ACCESS_INTEGRATION

SHOW SECRETS LIKE 'GRAPH_API_SECRET' IN SCHEMA MEDSOCMS.APP_CATALOG;
SHOW NETWORK RULES LIKE 'GRAPH_API_NETWORK_RULE' IN SCHEMA MEDSOCMS.APP_CATALOG;
SHOW EXTERNAL ACCESS INTEGRATIONS LIKE 'GRAPH_API_ACCESS_INTEGRATION';

-- If any of the above return no rows, Graph API ingestion (SharePoint /
-- network drive) will fail until an admin creates them once, tenant-wide:
--
-- CREATE SECRET IF NOT EXISTS MEDSOCMS.APP_CATALOG.GRAPH_API_SECRET
--   TYPE = GENERIC_STRING
--   SECRET_STRING = '<client_secret>';
--
-- CREATE NETWORK RULE IF NOT EXISTS MEDSOCMS.APP_CATALOG.GRAPH_API_NETWORK_RULE
--   MODE = EGRESS
--   TYPE = HOST_PORT
--   VALUE_LIST = ('login.microsoftonline.com', 'graph.microsoft.com');
--
-- CREATE EXTERNAL ACCESS INTEGRATION IF NOT EXISTS GRAPH_API_ACCESS_INTEGRATION
--   ALLOWED_NETWORK_RULES = (MEDSOCMS.APP_CATALOG.GRAPH_API_NETWORK_RULE)
--   ALLOWED_AUTHENTICATION_SECRETS = (MEDSOCMS.APP_CATALOG.GRAPH_API_SECRET)
--   ENABLED = TRUE;

-- ----------------------------------------------------------------------------
-- If/when LEX gets its own dedicated app registration + secret (recommended
-- in the README for least-privilege access to just the contracts library),
-- run this once to create its secret object and allow-list it on the same
-- shared integration used above — do NOT create a second network rule or
-- integration for it, since the hostnames being called don't change:
--
-- CREATE SECRET IF NOT EXISTS MEDSOCMS.APP_CATALOG.LEX_GRAPH_API_SECRET
--   TYPE = GENERIC_STRING
--   SECRET_STRING = '<lex_client_secret>';
--
-- ALTER EXTERNAL ACCESS INTEGRATION GRAPH_API_ACCESS_INTEGRATION
--   SET ALLOWED_AUTHENTICATION_SECRETS = (
--     MEDSOCMS.APP_CATALOG.GRAPH_API_SECRET,
--     MEDSOCMS.APP_CATALOG.LEX_GRAPH_API_SECRET
--   );
--
-- GRANT USAGE ON SECRET MEDSOCMS.APP_CATALOG.LEX_GRAPH_API_SECRET TO ROLE ADVANCEDANALYTICS;
--
-- Then set on LEX's PROJECTS row:
--   UPDATE MEDSOCMS.APP_CATALOG.PROJECTS
--     SET GRAPH_TENANT_ID = '<lex_tenant_id>',
--         GRAPH_CLIENT_ID = '<lex_client_id>',
--         GRAPH_SECRET_NAME = 'MEDSOCMS.APP_CATALOG.LEX_GRAPH_API_SECRET'
--   WHERE PROJECT_CODE = 'LEX';
--
-- and the deploy notebook cell's SECRETS clause will bind that secret
-- instead of the shared one — see pipeline/00_provision_project.ipynb.
