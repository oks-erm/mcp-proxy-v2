"""Read plain string secrets (e.g. OAuth client_secret) from Secret Manager."""

from __future__ import annotations

import logging
import os

from google.cloud import secretmanager

logger = logging.getLogger(__name__)
_project_id = os.getenv("GCP_PROJECT_ID", "it-team-hw-project")


def fetch_secret_string(secret_id: str) -> str:
    if not secret_id:
        return ""
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{_project_id}/secrets/{secret_id}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("UTF-8").strip()
