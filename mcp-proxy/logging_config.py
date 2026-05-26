"""GCP-compatible logging configuration for Cloud Logging / Log Explorer."""

import logging
import os
import sys


def setup_logging() -> None:
    """
    Configure logging for GCP Cloud Logging compatibility.

    Uses StructuredLogHandler to write JSON-formatted logs to stdout,
    which Cloud Run / GKE captures and forwards to Cloud Logging.
    Severity, message, and structured fields are preserved for Log Explorer.
    """
    log_level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(log_level)

    # Clear existing handlers to avoid duplicates
    root.handlers.clear()

    try:
        from google.cloud.logging_v2.handlers.structured_log import StructuredLogHandler

        handler = StructuredLogHandler(stream=sys.stdout)
        handler.setLevel(log_level)
        root.addHandler(handler)
    except Exception:
        # Fallback on any error (ImportError, init failure in Cloud Run, etc.)
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(log_level)
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
