"""
Fetch instance metadata from the GCP metadata server.

The metadata server is only reachable inside GCP (Cloud Run, GCE, GKE, …).
Outside GCP (local dev, CI) the request will fail quickly and an empty string
is returned so callers can degrade gracefully.

The project ID is fetched at most once per process and then cached.
"""

from __future__ import annotations

import functools
import logging
import os
import urllib.request

_METADATA_URL = "http://metadata.google.internal/computeMetadata/v1/project/project-id"
_TIMEOUT_S = 1.0  # metadata server is local; fail fast outside GCP

logger = logging.getLogger(__name__)


@functools.cache
def get_project_id() -> str:
    """Return the GCP project ID, fetching it from the metadata server if needed.

    Returns an empty string when not running on GCP or when the fetch fails,
    so callers can skip building a full trace resource name without crashing.
    Result is cached for the lifetime of the process.
    """
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
    if project_id:
        return project_id

    req = urllib.request.Request(
        _METADATA_URL,
        headers={"Metadata-Flavor": "Google"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            return resp.read().decode().strip()
    except Exception as exc:
        logger.debug("Could not fetch project ID from metadata server: %s", exc)
        return ""
