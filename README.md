# starlette-gcp-logging

Structured JSON logging for [Starlette](https://www.starlette.io/) services
running on Google Cloud Run (or any GCP compute platform).

Provides two components that work together:

- **`GCPFormatter`** — a `logging.Formatter` that serialises every log record
  as a single-line JSON object understood by Cloud Logging, including severity
  mapping, source location, and trace/span correlation.
- **`GCPRequestLoggingMiddleware`** — a Starlette middleware that emits one
  structured log entry per request/response (with `httpRequest` metadata) and
  propagates trace context to every logger used inside that request via
  `contextvars` — no manual plumbing needed.

## Installation

```bash
pip install starlette-gcp-logging
```

## Quick start

```python
import logging
from starlette.applications import Starlette
import starlette_gcp_logging

# Wire up the formatter once at startup — all loggers inherit it.
handler = logging.StreamHandler()
handler.setFormatter(starlette_gcp_logging.GCPFormatter())   # project_id auto-detected on GCP
logging.basicConfig(handlers=[handler], level=logging.INFO)

app = Starlette(...)
app.add_middleware(starlette_gcp_logging.GCPRequestLoggingMiddleware)
```

With [uvicorn](https://www.uvicorn.org/):

```bash
uvicorn myapp:app --log-config /dev/null   # let GCPFormatter own the output
```

## Configuration

### `GCPFormatter(project_id="")`

| Parameter | Description |
|---|---|
| `project_id` | GCP project ID used to build the full trace resource name `projects/<id>/traces/<trace_id>`. When omitted (the default) it is fetched automatically from the [GCP instance metadata server](https://cloud.google.com/compute/docs/metadata/overview) on the first log call and cached for the lifetime of the process. Outside GCP the trace is still written — just without the project prefix. |

#### Log record fields emitted

| JSON key | Value |
|---|---|
| `severity` | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` |
| `message` | Formatted log message |
| `time` | RFC 3339 UTC timestamp |
| `logging.googleapis.com/sourceLocation` | `file`, `line`, `function` |
| `logging.googleapis.com/trace` | Full trace resource name (when a trace ID is present) |
| `logging.googleapis.com/spanId` | 16-char hex span ID |
| `logging.googleapis.com/traceSampled` | Boolean sampling flag |
| `exception` | Formatted traceback (when `exc_info` is set) |
| `@type` | GCP Error Reporting type URI (when `exc_info` is set) |
| _(extra keys)_ | Any fields passed via `extra={...}` to the logger |

### `GCPRequestLoggingMiddleware(app, *, project_id="", logger_name=..., default_level=logging.INFO)`

| Parameter | Description |
|---|---|
| `project_id` | Same as `GCPFormatter`. Auto-detected when omitted. |
| `logger_name` | Logger to write request entries to. Defaults to `starlette_gcp_logging.middleware`. |
| `default_level` | Log level for 1xx/2xx/3xx responses. 4xx → `WARNING`; 5xx → `ERROR`. |

#### Trace context extraction

The middleware reads incoming trace headers in this priority order:

1. **`traceparent`** (W3C / OpenTelemetry) — used when an upstream service
   propagates its own trace. Cloud Run appends its own span to this header, so
   it is always preferred when present.
2. **`X-Cloud-Trace-Context`** — GCP's own header, injected when there is no
   upstream `traceparent`.

The extracted trace ID and span ID are stored in `contextvars` for the
duration of the request, so every logger in the call stack automatically picks
them up without any explicit propagation.

## Accessing trace context manually

```python
from starlette_gcp_logging import formatter

# Inside a request handler:
print(formatter.request_trace.get())    # "projects/my-project/traces/abc123..."
print(formatter.request_span.get())     # "00f067aa0ba902b7"
```

## Project ID auto-detection

```python
from starlette_gcp_logging import _metadata

project = _metadata.get_project_id()  # fetches from metadata server, then cached
```

Outside GCP (local dev, CI) the metadata request times out after 1 second and
an empty string is returned — logging continues to work, just without the
project prefix in the trace resource name.
