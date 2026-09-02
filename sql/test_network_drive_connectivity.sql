-- ============================================================================
-- LEX : test_network_drive_connectivity.sql
-- Run once after setup to confirm the network drive's External Access
-- Integration works.
--
-- LEX's contracts library was confirmed to be a genuine on-prem network
-- drive (a Windows/SMB file share, reachable over TCP 445), not
-- SharePoint — so there is no Graph API, no OAuth token, and no Azure AD
-- app registration anywhere in this codebase. Instead, LEX has its own
-- dedicated secret (a Snowflake PASSWORD-type secret holding the service
-- account's username + password), network rule (allow-listing the file
-- server's host:445), and external access integration, all created below.
--
-- UNVERIFIED: written without a live SMB server or Snowflake account to
-- test against. If connectivity fails, the two most likely causes are
-- (1) Snowflake's outbound network only permits HTTPS egress, not raw
-- SMB/445 — confirm with whoever manages the Private Link / VPN
-- connectivity between Snowflake and the MTM network — and (2) the SMB
-- client library (smbprotocol) failing to resolve its compiled
-- dependency (cryptography) via this account's PyPI access integration.
-- ============================================================================

USE ROLE ADVANCEDANALYTICS;
USE WAREHOUSE MTMWH02;
USE DATABASE MEDSCOMA;

-- Expect these to already exist once the one-time setup below has been
-- run. All three are LEX's own — nothing here is shared with any other
-- project:
--   SECRET             MEDSCOMA.APP_CATALOG.LEX_NETWORK_DRIVE_SECRET
--   NETWORK RULE        MEDSCOMA.APP_CATALOG.LEX_NETWORK_DRIVE_NETWORK_RULE
--   EXTERNAL ACCESS      LEX_NETWORK_DRIVE_ACCESS_INTEGRATION

SHOW SECRETS LIKE 'LEX_NETWORK_DRIVE_SECRET' IN SCHEMA MEDSCOMA.APP_CATALOG;
SHOW NETWORK RULES LIKE 'LEX_NETWORK_DRIVE_NETWORK_RULE' IN SCHEMA MEDSCOMA.APP_CATALOG;
SHOW EXTERNAL ACCESS INTEGRATIONS LIKE 'LEX_NETWORK_DRIVE_ACCESS_INTEGRATION';

-- ----------------------------------------------------------------------------
-- One-time setup: run once, before LEX's Data Sources page can reach the
-- network drive. Replace <fileserver-host> with the actual hostname or IP
-- of the SMB file server (the same value that goes into
-- PROJECTS.NETWORK_DRIVE_HOST), and the placeholder username/password
-- with the service account's real credentials. Typically
-- SYSADMIN/ACCOUNTADMIN to create a SECRET/NETWORK RULE/EXTERNAL ACCESS
-- INTEGRATION — the same gotcha this template's own README documents for
-- compute pools.
-- ----------------------------------------------------------------------------

CREATE SECRET IF NOT EXISTS MEDSCOMA.APP_CATALOG.LEX_NETWORK_DRIVE_SECRET
  TYPE = PASSWORD
  USERNAME = '<service_account_username>'
  PASSWORD = '<service_account_password>';

CREATE NETWORK RULE IF NOT EXISTS MEDSCOMA.APP_CATALOG.LEX_NETWORK_DRIVE_NETWORK_RULE
  MODE = EGRESS
  TYPE = HOST_PORT
  VALUE_LIST = ('<fileserver-host>:445');

CREATE EXTERNAL ACCESS INTEGRATION IF NOT EXISTS LEX_NETWORK_DRIVE_ACCESS_INTEGRATION
  ALLOWED_NETWORK_RULES = (MEDSCOMA.APP_CATALOG.LEX_NETWORK_DRIVE_NETWORK_RULE)
  ALLOWED_AUTHENTICATION_SECRETS = (MEDSCOMA.APP_CATALOG.LEX_NETWORK_DRIVE_SECRET)
  ENABLED = TRUE;

-- Both grants below are typically needed alongside the CREATE statements
-- above (same SYSADMIN/ACCOUNTADMIN privilege, so worth bundling into one
-- ask): ADVANCEDANALYTICS needs USAGE on the SECRET to bind it in the
-- Streamlit app's SECRETS clause, and separately USAGE on the INTEGRATION
-- itself to reference it in EXTERNAL_ACCESS_INTEGRATIONS at all — without
-- the second grant, CREATE STREAMLIT fails with the same "does not exist
-- or not authorized" error the integration itself throws when missing.
GRANT USAGE ON SECRET MEDSCOMA.APP_CATALOG.LEX_NETWORK_DRIVE_SECRET TO ROLE ADVANCEDANALYTICS;
GRANT USAGE ON INTEGRATION LEX_NETWORK_DRIVE_ACCESS_INTEGRATION TO ROLE ADVANCEDANALYTICS;

-- Then set LEX's network drive host/share (if not already set at project
-- creation), optional default subfolder/domain, and the secret's
-- fully-qualified name on its PROJECTS row — config.py's
-- resolved_network_drive_secret_name raises a clear error until this is
-- populated:
--
--   UPDATE MEDSCOMA.APP_CATALOG.PROJECTS
--     SET NETWORK_DRIVE_HOST = '<fileserver-host>',
--         NETWORK_DRIVE_SHARE = '<share-name>',
--         NETWORK_DRIVE_DEFAULT_PATH = '<optional subfolder>',
--         NETWORK_DRIVE_DOMAIN = '<optional NTLM domain>',
--         NETWORK_DRIVE_SECRET_NAME = 'MEDSCOMA.APP_CATALOG.LEX_NETWORK_DRIVE_SECRET'
--   WHERE PROJECT_CODE = 'LEX';
--
-- and the deploy notebook cell's SECRETS clause will bind that secret
-- under the app's fixed 'network_drive_credential' alias — see
-- pipeline/00_provision_project.ipynb.
