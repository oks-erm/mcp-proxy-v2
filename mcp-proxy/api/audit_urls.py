"""Build Cloud Console Log Explorer URLs for audit log review."""

import re
import urllib.parse

import config


def log_explorer_audit_url() -> str:
    """
    Read-only link to Log Explorer with a filter for MCP Proxy audit lines.
    Operators can refine the query in the console.
    """
    project = config.GCP_PROJECT_ID or ""
    query = 'resource.type="cloud_run_revision" ' '(jsonPayload.audit=true OR textPayload=~"audit:")'
    return "https://console.cloud.google.com/logs/query?" + urllib.parse.urlencode({"project": project, "query": query})


def log_explorer_user_activity_url(user_id: str) -> str:
    """
    Log Explorer URL scoped to lines that mention this user (MCP auth decisions, audit JSON, etc.).
    Uses jsonPayload when Cloud Logging parses structured fields, else textPayload regex on audit lines.
    """
    project = config.GCP_PROJECT_ID or ""
    uid = (user_id or "").strip()
    if not uid:
        return log_explorer_audit_url()
    uid_json = uid.replace("\\", "\\\\").replace('"', '\\"')
    uid_re = re.escape(uid)
    query = (
        'resource.type="cloud_run_revision" '
        f'((jsonPayload.audit=true AND jsonPayload.user_id="{uid_json}") '
        f'OR (textPayload=~"audit:" AND textPayload=~"{uid_re}"))'
    )
    return "https://console.cloud.google.com/logs/query?" + urllib.parse.urlencode({"project": project, "query": query})
