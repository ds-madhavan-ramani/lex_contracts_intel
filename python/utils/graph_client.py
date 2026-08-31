"""
graph_client.py — generalized Microsoft Graph API client for SharePoint.

Unchanged from the project-llm-wiki template (ds-madhavan-ramani/org_mm_chat).
Does one generic thing well: given ANY SharePoint folder URL, resolve it to
a Graph drive/item and list its files (optionally recursive). Takes
tenant_id/client_id as plain parameters rather than importing them itself,
so a project with its own dedicated app registration (see config.py's
ProjectConfig.resolved_graph_tenant_id/resolved_graph_client_id) just
passes different values in — no change needed here either way.
"""

import re
import time
from dataclasses import dataclass
from typing import Callable, List, Optional
from urllib.parse import urlparse, unquote

import requests

from utils.logging_utils import get_logger

logger = get_logger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
MAX_RETRIES = 3


class GraphError(Exception):
    pass


@dataclass
class DriveItem:
    item_id: str
    name: str
    path: str            # human-readable path, for display in Streamlit
    is_folder: bool
    size_bytes: int = 0
    last_modified: Optional[str] = None
    web_url: Optional[str] = None   # SharePoint Online view URL — for citation links


def _get_access_token(tenant_id: str, client_id: str, client_secret: str) -> str:
    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    }
    resp = requests.post(url, data=data, timeout=30)
    if resp.status_code != 200:
        raise GraphError(f"Token request failed ({resp.status_code}): {resp.text[:300]}")
    return resp.json()["access_token"]


def _graph_get(url: str, token: str) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429:  # rate limited
            wait = int(resp.headers.get("Retry-After", 2 ** attempt))
            logger.warning("EVENT=GRAPH_RATE_LIMIT wait_s=%d attempt=%d", wait, attempt)
            time.sleep(wait)
            last_err = "429 rate limited"
            continue
        last_err = f"{resp.status_code}: {resp.text[:300]}"
        if attempt < MAX_RETRIES:
            time.sleep(2 ** attempt)
    raise GraphError(f"GET {url} failed after {MAX_RETRIES} attempts: {last_err}")


def parse_sharepoint_folder_url(folder_url: str) -> dict:
    """
    Accepts a SharePoint folder URL as copy-pasted from a browser, e.g.:
      https://metrotrains.sharepoint.com/:f:/s/<site>/<share_token>?e=...
      https://metrotrains.sharepoint.com/sites/<site>/Shared Documents/.../
    Returns {"hostname": ..., "site_path": "/sites/<site>", "raw_path": ...}
    for use with Graph's `sites/{hostname}:{site_path}` and shares endpoints.

    SharePoint "share" links (the ':f:/s/...' form most users copy) require
    resolving via the /shares/{encoded_url} Graph endpoint rather than parsing
    the path directly — that resolution happens in resolve_folder(), this
    function only extracts what can be read from the URL itself.
    """
    parsed = urlparse(folder_url)
    if not parsed.hostname or "sharepoint.com" not in parsed.hostname:
        raise GraphError(f"Not a recognizable SharePoint URL: {folder_url}")

    site_match = re.search(r"/sites/([^/]+)", unquote(parsed.path))
    site_path = f"/sites/{site_match.group(1)}" if site_match else None

    return {
        "hostname": parsed.hostname,
        "site_path": site_path,
        "original_url": folder_url,
    }


def resolve_folder(token: str, folder_url: str) -> DriveItem:
    """
    Resolves any SharePoint folder URL (share-link or direct path form) to a
    Graph DriveItem via the /shares endpoint, which Graph supports for both
    URL styles.
    """
    import base64

    encoded = base64.urlsafe_b64encode(folder_url.encode()).decode().rstrip("=")
    share_id = f"u!{encoded}"
    data = _graph_get(f"{GRAPH_BASE}/shares/{share_id}/driveItem", token)

    return DriveItem(
        item_id=data["id"],
        name=data.get("name", ""),
        path=data.get("parentReference", {}).get("path", "") + "/" + data.get("name", ""),
        is_folder="folder" in data,
        size_bytes=data.get("size", 0),
        last_modified=data.get("lastModifiedDateTime"),
        web_url=data.get("webUrl"),
    )


def list_folder(
    token: str,
    drive_id: str,
    item_id: str,
    recursive: bool = True,
    select_fn: Optional[Callable[[List[DriveItem]], List[DriveItem]]] = None,
) -> List[DriveItem]:
    """
    Lists files under a folder DriveItem. Recurses into subfolders by default
    (a contracts library is commonly organized into year/client/type
    subfolders, but this module makes no assumption about naming).

    select_fn: optional callable a project can pass to apply project-specific
    "pick the best file per folder" logic — the default (None) returns every
    file and lets the user choose in the UI.
    """
    files: List[DriveItem] = []
    data = _graph_get(f"{GRAPH_BASE}/drives/{drive_id}/items/{item_id}/children", token)

    folder_children = []
    for entry in data.get("value", []):
        item = DriveItem(
            item_id=entry["id"],
            name=entry["name"],
            path=entry.get("parentReference", {}).get("path", "") + "/" + entry["name"],
            is_folder="folder" in entry,
            size_bytes=entry.get("size", 0),
            last_modified=entry.get("lastModifiedDateTime"),
            web_url=entry.get("webUrl"),
        )
        if item.is_folder:
            folder_children.append(item)
        else:
            files.append(item)

    if select_fn:
        files = select_fn(files)

    if recursive:
        for folder in folder_children:
            files.extend(list_folder(token, drive_id, folder.item_id,
                                     recursive=True, select_fn=select_fn))

    return files


def download_file(token: str, drive_id: str, item_id: str) -> bytes:
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{GRAPH_BASE}/drives/{drive_id}/items/{item_id}/content"
    resp = requests.get(url, headers=headers, timeout=60)
    if resp.status_code not in (200, 302):
        raise GraphError(f"Download failed ({resp.status_code}) for item {item_id}")
    return resp.content


def get_client_secret(alias: str = "graph_secret") -> str:
    """
    Reads a Graph API client secret bound to this app under the given local
    alias by the deploy cell's SECRETS = ('graph_secret' = <secret object>)
    clause — see pipeline/00_provision_project.ipynb. The alias is always
    'graph_secret' regardless of which actual secret object it's bound to
    (shared tenant-level, or a project's own dedicated one), so callers
    never need to know that detail.

    Container runtime and warehouse runtime expose bound secrets through
    different, mutually-exclusive APIs: container runtime (what this app
    actually runs on) has no _snowflake module and exposes secrets through
    st.secrets[alias] (also mirrored into os.environ), while warehouse
    runtime is the reverse, via _snowflake.get_generic_secret_string(alias).
    Try st.secrets first, falling back to _snowflake in case warehouse
    runtime is ever used.
    """
    try:
        import streamlit as st
        return st.secrets[alias]
    except Exception:
        import _snowflake
        return _snowflake.get_generic_secret_string(alias)
