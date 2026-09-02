"""
network_drive_client.py — SMB/CIFS client for LEX's on-prem network drive.

Replaces this template's usual Microsoft Graph API / SharePoint ingestion
path entirely: LEX's contracts library was confirmed to be a genuine
network file share (a UNC path over SMB), not SharePoint, so there is no
Graph API, no OAuth token, and no Azure AD app registration anywhere in
this codebase.

Built on `smbprotocol` (PyPI) via its `smbclient` submodule, which exposes
an os/os.path-like API (listdir, stat, open_file) over SMB2/3 — needed
since modern Windows Server file shares generally reject the older SMB1
that simpler libraries like pysmb assume.

Requires LEX's network drive host to be reachable from the Streamlit
container runtime's outbound network — a Snowflake External Access
Integration network rule for <host>:445 (see
sql/test_network_drive_connectivity.sql), which in turn requires the
existing Private Link / VPN connectivity between Snowflake and the MTM
network (per the architecture doc) to actually route raw SMB traffic, not
just HTTPS.

UNVERIFIED: written without a live SMB server, Snowflake account, or
container runtime to test against — treat this as a best-effort starting
point, not confirmed-working code. If it doesn't connect, the two most
likely culprits are (1) the network path only permitting HTTPS egress,
not raw SMB/445, and (2) smbprotocol's compiled dependency (cryptography)
failing to resolve via this account's PyPI access integration, the same
class of failure other packages have hit elsewhere in this codebase's
history.
"""

import os
from dataclasses import dataclass
from typing import List, Optional

import smbclient
import smbclient.path

from utils.logging_utils import get_logger

logger = get_logger(__name__)

_registered_hosts = set()


class NetworkDriveError(Exception):
    pass


@dataclass
class NetworkDriveItem:
    item_id: str              # the file's UNC path — a stable per-file identity
    name: str
    path: str
    size_bytes: int = 0
    last_modified: Optional[float] = None


def get_network_drive_credentials(alias: str = "network_drive_credential"):
    """
    Reads the username/password pair bound to this app under the given
    local alias by the deploy cell's SECRETS = ('network_drive_credential'
    = <PASSWORD-type secret>) clause — see
    pipeline/00_provision_project.ipynb. Container runtime (what this app
    runs on) exposes a PASSWORD-type secret as st.secrets[alias] with
    username/password fields; warehouse runtime's equivalent,
    _snowflake.get_username_password_secret(alias), is tried as a fallback
    in case warehouse runtime is ever used.

    UNVERIFIED for a PASSWORD-type secret specifically (GENERIC_STRING
    secrets, used elsewhere in this codebase for the old Graph API path,
    are confirmed working) — if this fails, check Snowflake's current docs
    for exactly how a PASSWORD-type secret surfaces under st.secrets, and
    adjust the attribute/key access below.

    Falls through to NETWORK_DRIVE_USERNAME/NETWORK_DRIVE_PASSWORD
    environment variables as a third option, for standalone scripts that
    run entirely outside Snowflake/Streamlit — e.g.
    pipeline/network_drive_to_stage.py, a bridge agent run from inside the
    MTM network while direct Snowflake-to-network-drive connectivity is
    still being sorted out with the Networks team.
    """
    try:
        import streamlit as st
        cred = st.secrets[alias]
        username = cred["username"] if isinstance(cred, dict) else cred.username
        password = cred["password"] if isinstance(cred, dict) else cred.password
        return username, password
    except Exception:
        pass

    try:
        import _snowflake
        cred = _snowflake.get_username_password_secret(alias)
        return cred.username, cred.password
    except Exception:
        pass

    username = os.environ.get("NETWORK_DRIVE_USERNAME")
    password = os.environ.get("NETWORK_DRIVE_PASSWORD")
    if username and password:
        return username, password

    raise NetworkDriveError(
        "Could not resolve network drive credentials from st.secrets, "
        "_snowflake, or the NETWORK_DRIVE_USERNAME/NETWORK_DRIVE_PASSWORD "
        "environment variables."
    )


def _ensure_registered(project) -> None:
    host = project.network_drive_host
    if host in _registered_hosts:
        return
    username, password = get_network_drive_credentials()
    smbclient.register_session(
        host, username=username, password=password,
        domain=project.network_drive_domain or "",
    )
    _registered_hosts.add(host)


def _unc_path(project, relative_path: str = "") -> str:
    base = f"\\\\{project.network_drive_host}\\{project.network_drive_share}"
    sub = (relative_path or project.network_drive_default_path or "").strip("\\/")
    return f"{base}\\{sub}" if sub else base


def list_files(project, relative_path: str = "", recursive: bool = True) -> List[NetworkDriveItem]:
    """Lists files under the share (optionally a subfolder), recursively by
    default — a contracts library is commonly organized into
    year/client/type subfolders."""
    _ensure_registered(project)
    root = _unc_path(project, relative_path)
    return _list_dir(root, recursive)


def _list_dir(root: str, recursive: bool) -> List[NetworkDriveItem]:
    results: List[NetworkDriveItem] = []
    try:
        entries = smbclient.listdir(root)
    except Exception as e:
        raise NetworkDriveError(f"Could not list '{root}': {e}") from e

    for name in entries:
        full = f"{root}\\{name}"
        try:
            if smbclient.path.isdir(full):
                if recursive:
                    results.extend(_list_dir(full, recursive=True))
                continue
            info = smbclient.stat(full)
            results.append(NetworkDriveItem(
                item_id=full, name=name, path=full,
                size_bytes=getattr(info, "st_size", 0),
                last_modified=getattr(info, "st_mtime", None),
            ))
        except Exception as e:  # noqa: BLE001
            logger.warning("EVENT=NETWORK_DRIVE_LIST_ITEM_FAILED path=%s error=%s", full, e)

    return results


def download_file(project, item_path: str) -> bytes:
    _ensure_registered(project)
    try:
        with smbclient.open_file(item_path, mode="rb") as f:
            return f.read()
    except Exception as e:
        raise NetworkDriveError(f"Could not download '{item_path}': {e}") from e
