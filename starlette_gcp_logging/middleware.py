"""
Starlette middleware that emits one structured log entry per HTTP
request/response in a format compatible with Google Cloud Logging.

Trace context is extracted from the incoming request headers
(``X-Cloud-Trace-Context`` for GCP-native, ``traceparent`` for W3C) and
stored in module-level ContextVars so that *every* logger used anywhere
inside the request handler automatically includes the correct trace and
span IDs — no manual propagation needed.

The request log entry includes an ``httpRequest`` payload that Cloud Logging
can parse and surface in the Logs Explorer HTTP-request view.
"""

import collections.abc
import logging
import time
import typing

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Match
from starlette.routing import get_route_path
from starlette.types import ASGIApp

from . import _metadata
from . import formatter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Trace-context helpers
# ---------------------------------------------------------------------------


def _parse_xctc(header: str) -> tuple[str, str, bool]:
    """Parse ``X-Cloud-Trace-Context: TRACE_ID[/SPAN_ID][;o=FLAG]``.

    SPAN_ID in this header is a uint64 decimal; GCP logging expects a
    16-character lowercase hex string, so we convert it.
    """
    trace_id = ""
    span_id = ""
    sampled = False

    slash = header.find("/")
    if slash == -1:
        trace_id = header.strip()
        return trace_id, span_id, sampled

    trace_id = header[:slash]
    remainder = header[slash + 1 :]

    semicolon = remainder.find(";")
    if semicolon == -1:
        span_raw = remainder
    else:
        span_raw = remainder[:semicolon]
        opts = remainder[semicolon + 1 :]
        for opt in opts.split(";"):
            if opt.startswith("o="):
                sampled = opt[2:] == "1"

    # Convert decimal span to 16-char hex expected by Cloud Logging
    if span_raw:
        try:
            span_id = format(int(span_raw), "016x")
        except ValueError:
            span_id = span_raw  # pass through if already hex / unexpected format

    return trace_id, span_id, sampled


def _parse_traceparent(header: str) -> tuple[str, str, bool]:
    """Parse W3C ``traceparent: VER-TRACE_ID-SPAN_ID-FLAGS``."""
    parts = header.split("-")
    if len(parts) < 4:
        return "", "", False
    try:
        sampled = (int(parts[3], 16) & 0x01) == 1
    except ValueError:
        sampled = False
    return parts[1], parts[2], sampled


def _extract_iap_user_email(request: Request) -> str:
    """Return the authenticated user email from IAP headers, or ``""`` if absent.

    The only supported source is ``x-goog-authenticated-user-email``.
    Values are often prefixed with ``accounts.google.com:``; that prefix
    is stripped before returning.

    ``x-serverless-authorization`` is intentionally ignored because it carries
    an opaque bearer token, not an email address, and logging it would leak
    credential-like data.
    """
    email_header = request.headers.get("x-goog-authenticated-user-email")
    if email_header:
        # Strip the identity-provider prefix, e.g. "accounts.google.com:user@example.com"
        _, _, email = email_header.partition(":")
        return email or email_header

    return ""


def _extract_trace_context(request: Request) -> tuple[str, str, bool]:
    """Return ``(trace_id, span_id, sampled)`` from the best available header.

    Priority:
    1. ``traceparent`` (W3C / OpenTelemetry standard) — used when an upstream
       service propagates its own trace context.
    2. ``X-Cloud-Trace-Context`` — injected by GCP infrastructure when no
       upstream trace is present.
    """
    tp = request.headers.get("traceparent")
    if tp:
        return _parse_traceparent(tp)

    xctc = request.headers.get("x-cloud-trace-context")
    if xctc:
        return _parse_xctc(xctc)

    return "", "", False


# ---------------------------------------------------------------------------
# Route-template helpers
# ---------------------------------------------------------------------------


def _best(
    candidates: collections.abc.Iterable[tuple[Match, str | None]],
) -> tuple[Match, str | None]:
    """Return the first full match, else the first partial one.

    This is how Starlette's router picks a route: a partial match (the path
    matches but the method doesn't) is only used if no later route matches
    fully.
    """
    partial: str | None = None
    for match, template in candidates:
        if match == Match.FULL:
            # Routing stops here, even if this is a Mount with no matching
            # child (which returns 404) and so has no template.
            return Match.FULL, template
        if match == Match.PARTIAL and template is not None and partial is None:
            partial = template
    if partial is not None:
        return Match.PARTIAL, partial
    return Match.NONE, None


def _match_route(
    route: typing.Any, scope: collections.abc.Mapping[str, typing.Any]
) -> tuple[Match, str | None]:
    """Match a single Starlette route (leaf or Mount) against *scope*."""
    match, child_scope = route.matches(scope)
    if match == Match.NONE:
        return Match.NONE, None
    if hasattr(route, "endpoint"):
        # Leaf Route / WebSocketRoute — its path template.
        return match, route.path
    if callable(getattr(route, "effective_route_contexts", None)):
        return _match_included_router(route, scope)
    # Mount or Host — recurse with the child scope so that get_route_path
    # trims the mount prefix before inner routes try to match. Routing enters
    # a matching Mount even if no inner route matches fully (and returns 405),
    # so the container's own match decides. A Host has no path prefix.
    _, sub = _match_router(getattr(route, "app", None), {**scope, **child_scope})
    if sub is None:
        return match, None
    return match, getattr(route, "path", "") + sub


def _match_included_router(
    included_router: typing.Any, scope: collections.abc.Mapping[str, typing.Any]
) -> tuple[Match, str | None]:
    """Match the routes of a FastAPI included router against *scope*.

    Since FastAPI 0.137, ``include_router`` adds a single ``_IncludedRouter``
    entry that keeps the original routes, rather than copies of them with the
    prefix applied. Each of its effective route contexts carries the full
    path, with every ``include_router`` prefix, and the path regex and
    methods the route matches with once included. Non-API routes, such as
    mounts, come with a ``starlette_route`` already rebuilt with the prefix.
    """
    # get_route_path only reads the scope, though it's typed as mutable.
    route_path = get_route_path(typing.cast(dict[str, typing.Any], scope))

    def candidates() -> collections.abc.Iterator[tuple[Match, str | None]]:
        for context in included_router.effective_route_contexts():
            starlette_route = getattr(context, "starlette_route", None)
            if starlette_route is not None:
                yield _match_route(starlette_route, scope)
                continue
            path_regex = getattr(context, "path_regex", None)
            if path_regex is None or not path_regex.match(route_path):
                continue
            methods = getattr(context, "methods", None)
            if not methods or scope.get("method") in methods:
                yield Match.FULL, context.path
            else:
                yield Match.PARTIAL, context.path

    return _best(candidates())


def _match_router(
    router: typing.Any, scope: collections.abc.Mapping[str, typing.Any]
) -> tuple[Match, str | None]:
    return _best(_match_route(route, scope) for route in getattr(router, "routes", []))


def _find_route_template(
    router: typing.Any, scope: collections.abc.Mapping[str, typing.Any]
) -> str | None:
    """Recursively search *router*'s route table for the path template that
    matches *scope*, e.g. ``'/api/user/{user_id}/task/{task_id}'``.

    Returns ``None`` when no route matches (e.g. a 404 request). As in
    Starlette's router, a route that matches the path but not the method is
    only used when no other route matches both.

    ``route.matches()`` is a pure query — it does not mutate *scope* — so
    calling it before the actual routing happens in ``call_next`` is safe.
    When a :class:`starlette.routing.Mount` matches, its ``child_scope``
    carries an updated ``root_path`` that trims the mount prefix; we merge
    this into a copy of *scope* before recursing so that inner routes see
    only their own path segment. Routers added with FastAPI's
    ``include_router`` are searched through their effective route contexts.
    """
    return _match_router(router, scope)[1]


def _extract_route_path(request: Request) -> str:
    """Return the matched route path template including any ``root_path``.

    For example, given a request to ``/api/user/123/task/abc`` matched by the
    route ``/api/user/{user_id}/task/{task_id}``, returns
    ``"/api/user/{user_id}/task/{task_id}"`` (or
    ``"/prefix/api/user/{user_id}/task/{task_id}"`` when ``root_path`` is set).
    Returns ``""`` when no route matches.
    """
    app = request.scope.get("app")
    template = _find_route_template(app, request.scope)
    if template is None:
        return ""
    root_path = request.scope.get("root_path", "")
    return root_path + template


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class GCPRequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log one structured entry per request/response to Google Cloud Logging.

    Parameters
    ----------
    app:
        The ASGI application to wrap.
    project_id:
        GCP project ID, used to build the full trace resource name stored in
        ``request_trace`` so that ``GCPFormatter`` can emit it verbatim.
    logger_name:
        Name of the Python logger to use. Defaults to this module's name.
    default_level:
        Log level used for 1xx/2xx/3xx responses. 4xx responses use WARNING;
        5xx responses use ERROR.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        project_id: str = "",
        logger_name: str = __name__,
        default_level: int = logging.INFO,
    ) -> None:
        super().__init__(app)
        # Resolve at startup (one blocking metadata call at most).
        self._project_id = project_id or _metadata.get_project_id()
        self._logger = logging.getLogger(logger_name)
        self._default_level = default_level

    async def dispatch(
        self,
        request: Request,
        call_next: collections.abc.Callable[
            [Request], collections.abc.Awaitable[Response]
        ],
    ) -> Response:
        trace_id, span_id, sampled = _extract_trace_context(request)
        user_email = _extract_iap_user_email(request)
        route_path = _extract_route_path(request)

        # Store the bare trace ID; GCPFormatter is responsible for building
        # the full "projects/<id>/traces/<trace_id>" resource name when it has
        # a project_id configured.
        trace_tok = formatter.request_trace.set(trace_id)
        span_tok = formatter.request_span.set(span_id)
        sampled_tok = formatter.request_trace_sampled.set(sampled)
        email_tok = formatter.request_user_email.set(user_email)
        route_tok = formatter.request_route.set(route_path)

        status_code = 500
        start = time.perf_counter()
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception:
            self._logger.exception(
                "Unhandled exception processing %s %s",
                request.method,
                request.url.path,
            )
            raise
        finally:
            latency_s = time.perf_counter() - start
            self._emit(request, status_code, latency_s)
            formatter.request_trace.reset(trace_tok)
            formatter.request_span.reset(span_tok)
            formatter.request_trace_sampled.reset(sampled_tok)
            formatter.request_user_email.reset(email_tok)
            formatter.request_route.reset(route_tok)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _level_for_status(self, status_code: int) -> int:
        if status_code >= 500:
            return logging.ERROR
        if status_code >= 400:
            return logging.WARNING
        return self._default_level

    def _emit(
        self,
        request: Request,
        status_code: int,
        latency_s: float,
    ) -> None:
        level = self._level_for_status(status_code)

        # Latency as a proto Duration string (e.g. "0.123456s")
        latency_str = f"{latency_s:.6f}s"

        remote_ip = ""
        if request.client:
            remote_ip = request.client.host

        # Prefer the de-facto forwarded-for header when behind a load balancer
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            remote_ip = forwarded_for.split(",")[0].strip()

        http_version = request.scope.get("http_version", "1.1")

        self._logger.log(
            level,
            "%s %s %d (%.3fs)",
            request.method,
            request.url.path,
            status_code,
            latency_s,
            extra={
                # Cloud Logging parses this key specially in structured logs
                "httpRequest": {
                    "requestMethod": request.method,
                    "requestUrl": str(request.url),
                    "status": status_code,
                    "userAgent": request.headers.get("user-agent", ""),
                    "remoteIp": remote_ip,
                    "protocol": f"HTTP/{http_version}",
                    "latency": latency_str,
                    "referer": request.headers.get("referer", ""),
                },
            },
        )
