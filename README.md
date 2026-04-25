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

## Example GCP alerts grouped by Starlette route

The middleware writes the route template to:

- `jsonPayload."logging.googleapis.com/labels"."starlette.dev/route"`

Use that field as a log-based metric label so alerts are grouped per route
template (for example, `"/items/{item_id}"`).

### 1) 4xx error-rate alert by route

Create two log-based **counter** metrics with the same label extractor:

- `logging.googleapis.com/user/starlette_requests_total`
- `logging.googleapis.com/user/starlette_requests_4xx`

Filter / extractor examples:

```text
# total requests
resource.type="cloud_run_revision"
jsonPayload.httpRequest.status>=100
jsonPayload.httpRequest.status<600
jsonPayload."logging.googleapis.com/labels"."starlette.dev/route"!=""

labelExtractors.route=EXTRACT(jsonPayload."logging.googleapis.com/labels"."starlette.dev/route")
```

```text
# 4xx requests
resource.type="cloud_run_revision"
jsonPayload.httpRequest.status>=400
jsonPayload.httpRequest.status<500
jsonPayload."logging.googleapis.com/labels"."starlette.dev/route"!=""

labelExtractors.route=EXTRACT(jsonPayload."logging.googleapis.com/labels"."starlette.dev/route")
```

Alerting condition (MQL example, threshold 5% over 5m):

```text
fetch logging.googleapis.com/user/starlette_requests_4xx
| align rate(5m)
| group_by [metric.route], [err: sum(value.rate)]
| join
    (fetch logging.googleapis.com/user/starlette_requests_total
     | align rate(5m)
     | group_by [metric.route], [all: sum(value.rate)])
| value [error_rate: err / all]
| condition error_rate > 0.05 '1'
```

### 2) 5xx error-rate alert by route

Create a `logging.googleapis.com/user/starlette_requests_5xx` counter metric:

```text
resource.type="cloud_run_revision"
jsonPayload.httpRequest.status>=500
jsonPayload.httpRequest.status<600
jsonPayload."logging.googleapis.com/labels"."starlette.dev/route"!=""

labelExtractors.route=EXTRACT(jsonPayload."logging.googleapis.com/labels"."starlette.dev/route")
```

Alerting condition (MQL example, threshold 1% over 5m):

```text
fetch logging.googleapis.com/user/starlette_requests_5xx
| align rate(5m)
| group_by [metric.route], [err: sum(value.rate)]
| join
    (fetch logging.googleapis.com/user/starlette_requests_total
     | align rate(5m)
     | group_by [metric.route], [all: sum(value.rate)])
| value [error_rate: err / all]
| condition error_rate > 0.01 '1'
```

### 3) Successful-request latency alert by route

Create a log-based **distribution** metric
`logging.googleapis.com/user/starlette_success_latency_seconds`:

```text
resource.type="cloud_run_revision"
jsonPayload.httpRequest.status>=200
jsonPayload.httpRequest.status<400
jsonPayload.httpRequest.latency=~".*s"
jsonPayload."logging.googleapis.com/labels"."starlette.dev/route"!=""

labelExtractors.route=EXTRACT(jsonPayload."logging.googleapis.com/labels"."starlette.dev/route")
valueExtractor=REGEXP_EXTRACT(jsonPayload.httpRequest.latency, "^([0-9.]+)s$")
```

Alerting condition (MQL example, p95 latency > 1s over 5m):

```text
fetch logging.googleapis.com/user/starlette_success_latency_seconds
| group_by 5m, [metric.route], [p95: percentile(value.distribution, 95)]
| condition p95 > 1
```
