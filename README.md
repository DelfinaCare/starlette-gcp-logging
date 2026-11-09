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
handler.setFormatter(
    starlette_gcp_logging.GCPFormatter()
)  # project_id auto-detected on GCP
logging.basicConfig(handlers=[handler], level=logging.INFO)

app = Starlette(...)
app.add_middleware(starlette_gcp_logging.GCPRequestLoggingMiddleware)
```

With [uvicorn](https://www.uvicorn.org/):

```bash
uvicorn myapp:app --log-config /dev/null   # let GCPFormatter own the output
```

## Configuration

### `GCPFormatter(project_id="", *, labels=None, fields=None)`

| Parameter | Description |
|---|---|
| `project_id` | GCP project ID used to build the full trace resource name `projects/<id>/traces/<trace_id>`. When omitted (the default) it is fetched automatically from the [GCP instance metadata server](https://cloud.google.com/compute/docs/metadata/overview) on the first log call and cached for the lifetime of the process. Outside GCP the trace is still written — just without the project prefix. |
| `labels` | Optional mapping of custom GCP label name → the `ContextVar` its value should be read from. See [Custom labels](#custom-labels) below. |
| `fields` | Optional callable `(record) -> Mapping | None` that returns extra top-level JSON fields for every log entry. See [Custom fields](#custom-fields) below. |

#### Log record fields emitted

| JSON key | Value |
|---|---|
| `severity` | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` |
| `message` | Formatted log message |
| `time` | RFC 3339 UTC timestamp |
| `logging.googleapis.com/sourceLocation` | `file`, `line`, `function` |
| `logging.googleapis.com/trace` | Full trace resource name (when a trace ID is present) |
| `logging.googleapis.com/spanId` | 16-char hex span ID |
| `logging.googleapis.com/trace_sampled` | Boolean sampling flag |
| `exception` | Formatted traceback (when `exc_info` is set) |
| `@type` | GCP Error Reporting type URI (when `exc_info` is set) |
| _(extra keys)_ | Fields from the `fields` callable, `extra={"json_fields": {...}}` and `extra={...}`. See [Custom fields](#custom-fields). |

### `GCPRequestLoggingMiddleware(app, *, project_id="", logger_name=..., default_level=logging.INFO)`

| Parameter | Description |
|---|---|
| `project_id` | Same as `GCPFormatter`. Auto-detected when omitted. |
| `logger_name` | Logger to write request entries to. Defaults to `starlette_gcp_logging.middleware`. |
| `default_level` | Log level for 1xx/2xx/3xx responses. 4xx → `WARNING`; 5xx → `ERROR`. |

### Custom labels

Application code often stores per-request data (tenant ID, feature flag,
API key ID, ...) in its own `contextvars.ContextVar`. Pass a `labels` mapping
to `GCPFormatter` to surface any of those as GCP labels on *every* log entry
emitted while the variable holds a value:

```python
import contextvars
import logging
import starlette_gcp_logging

tenant_id: contextvars.ContextVar[str] = contextvars.ContextVar("tenant_id", default="")

handler = logging.StreamHandler()
handler.setFormatter(
    starlette_gcp_logging.GCPFormatter(labels={"tenant_id": tenant_id})
)
logging.basicConfig(handlers=[handler], level=logging.INFO)
```

Somewhere earlier in the middleware stack (or in a dependency), set the
variable for the duration of the request:

```python
tok = tenant_id.set("acme-corp")
try:
    ...
finally:
    tenant_id.reset(tok)
```

Every log entry emitted while `tenant_id` holds a value (including falsy
values like `False`, `0`, or `""`) will include:

```
labels."tenant_id" = "acme-corp"
```

Cloud Logging labels are strings, so non-string values are converted:
`True`/`False` become `"true"`/`"false"` and anything else goes through
`str()`. Labels set in the request handler appear on the log entries the
handler emits, but not on the middleware's request entry, which is written
from the middleware's own context.

A label is only omitted when its `ContextVar` has not been `.set()` in the
current context (i.e. it is unset), matching the behavior of the built-in
`authenticated_user_email` and `starlette.dev/route` labels. This lets you
distinguish an explicitly-set falsy value (e.g. a `False` feature flag) from
an unset variable — unlike a truthiness check, `None` is the only value
treated as "unset".

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

#### OpenTelemetry

When `opentelemetry-api` is installed (`pip install
starlette-gcp-logging[opentelemetry]`) and a span is active, `GCPFormatter`
uses that span's trace ID, span ID and sampling flag instead of the header
values. Logs then link to the span that was actually exported (for example
the server span from `opentelemetry-instrumentation-fastapi`), and logs
written inside spans outside of a request, such as in background threads,
are correlated too.

### Custom fields

Cloud Logging stores every non-special key of a structured log line in
`jsonPayload`. There are three ways to add your own keys.

Per call, pass them with `extra`:

```python
logger.info("charged card", extra={"order_id": order_id, "amount": 12.5})
```

`extra` keys cannot use the name of a `LogRecord` attribute (`name`,
`module`, `args`, ...); the logging module raises `KeyError`. Pass those, or
any dict of fields, under `json_fields` instead, as with
[google-cloud-logging's `StructuredLogHandler`](https://cloud.google.com/python/docs/reference/logging/latest/std-lib-integration#log-structured-data).
They are written at the top level, not under a `json_fields` key:

```python
logger.info("created", extra={"json_fields": {"name": widget.name, "id": widget.id}})
```

For fields that belong on *every* log entry (the deployed commit, a request
ID, the calling account, ...), give `GCPFormatter` a `fields` callable. It is
called once per record, in the thread that logs, so it can read
`contextvars` and libraries built on them, such as `starlette_context`:

```python
import logging
import os

from starlette_context import context

import starlette_gcp_logging


def log_fields(record: logging.LogRecord) -> dict:
    fields = {"commit": os.environ.get("COMMIT_SHA", "")}
    if context.exists():
        fields["auditInfo"] = {
            key: context.data[key]
            for key in ("login_account_id", "iap_email")
            if key in context.data
        }
    return fields


handler = logging.StreamHandler()
handler.setFormatter(starlette_gcp_logging.GCPFormatter(fields=log_fields))
logging.basicConfig(handlers=[handler], level=logging.INFO)
```

Values can be anything JSON-serialisable, including nested dicts; other
objects are converted with `str()`. The callable may return `None` to add
nothing. If it raises, the logging module reports the error on stderr and
drops that record, so keep it simple.

When the same key comes from more than one place, the most specific wins:
`extra={...}` keys over `json_fields` over the `fields` callable. None of
them can replace a field the formatter writes itself for that record
(`severity`, `message`, `time`, `logger`, the trace and source location
fields, `logging.googleapis.com/labels`, `exception`, ...).

Prefer this over a `logging.Filter` that sets attributes on the record: a
filter changes the record for every handler, must be added to each handler
(filters on a logger do not run for records propagated from child loggers),
and cannot set fields named like `LogRecord` attributes.

Use [`labels`](#custom-labels) instead for short string values you want to
filter or build log-based metrics on; use `fields` for everything else.

## Accessing trace context manually

```python
from starlette_gcp_logging import formatter

# Inside a request handler:
print(formatter.request_trace.get())  # "projects/my-project/traces/abc123..."
print(formatter.request_span.get())  # "00f067aa0ba902b7"
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

Cloud Logging promotes the
[special JSON fields](https://cloud.google.com/logging/docs/structured-logging#special-payload-fields)
out of `jsonPayload` when it ingests a structured log line. On the stored
`LogEntry` the request data is under `httpRequest` and the labels are under
`labels`, so filters must use `httpRequest.status` and
`labels."starlette.dev/route"`, not `jsonPayload.httpRequest.status`.

The middleware writes the route template to:

- `labels."starlette.dev/route"`

Use that field as a log-based metric label so alerts are grouped per route
template (for example, `"/items/{item_id}"`). On Cloud Run, Cloud Run's own
request logs (`run.googleapis.com/requests`) also carry `httpRequest`, so
restrict the filters to the middleware's entries with
`jsonPayload.logger="starlette_gcp_logging.middleware"`.

### 1) 4xx error-rate alert by route

Create two log-based **counter** metrics with the same label extractor:

- `logging.googleapis.com/user/starlette_requests_total`
- `logging.googleapis.com/user/starlette_requests_4xx`

Filter / extractor examples:

```text
# total requests
resource.type="cloud_run_revision"
httpRequest.status>=100
httpRequest.status<600
jsonPayload.logger="starlette_gcp_logging.middleware"
labels."starlette.dev/route"!=""

labelExtractors.route=EXTRACT(labels."starlette.dev/route")
```

```text
# 4xx requests
resource.type="cloud_run_revision"
httpRequest.status>=400
httpRequest.status<500
jsonPayload.logger="starlette_gcp_logging.middleware"
labels."starlette.dev/route"!=""

labelExtractors.route=EXTRACT(labels."starlette.dev/route")
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
httpRequest.status>=500
httpRequest.status<600
jsonPayload.logger="starlette_gcp_logging.middleware"
labels."starlette.dev/route"!=""

labelExtractors.route=EXTRACT(labels."starlette.dev/route")
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
httpRequest.status>=200
httpRequest.status<400
httpRequest.latency=~".*s"
jsonPayload.logger="starlette_gcp_logging.middleware"
labels."starlette.dev/route"!=""

labelExtractors.route=EXTRACT(labels."starlette.dev/route")
valueExtractor=REGEXP_EXTRACT(httpRequest.latency, "^([0-9.]+)s$")
```

Alerting condition (MQL example, p95 latency > 1s over 5m):

```text
fetch logging.googleapis.com/user/starlette_success_latency_seconds
| group_by 5m, [metric.route], [p95: percentile(value.distribution, 95)]
| condition p95 > 1
```
