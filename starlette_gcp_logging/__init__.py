"""
starlette_gcp_logging — GCP-structured JSON logging for Starlette / Cloud Run.

Top-level exports
-----------------
    from starlette_gcp_logging import GCPFormatter, GCPRequestLoggingMiddleware

Submodules are also accessible directly for everything else:

    from starlette_gcp_logging import formatter, middleware, _metadata
"""

from . import _metadata
from . import formatter
from . import middleware

GCPFormatter = formatter.GCPFormatter
GCPRequestLoggingMiddleware = middleware.GCPRequestLoggingMiddleware

__all__ = [
    "GCPFormatter",
    "GCPRequestLoggingMiddleware",
    "_metadata",
    "formatter",
    "middleware",
]
