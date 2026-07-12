# Observability — OpenTelemetry tracing

HUNTER ships an **opt-in** OpenTelemetry tracing layer. It is fully gated behind
the `HUNT_OTEL` environment variable: when unset (the default) every helper is a
zero-overhead no-op, so the un-instrumented deployment behaves exactly as before.

## Files

| File | Role |
| --- | --- |
| `hunt_core/runtime/telemetry.py` | The whole tracing layer: `init_telemetry`, `span`, `set_attributes`, `traced`, `is_active`. |
| `hunt_core/bootstrap.py` | Calls `init_telemetry("hunt_core")` from `bootstrap()`, wrapped defensively so telemetry can never break startup. |
| `hunt_core/runtime/cycle/_cycle_loop.py` | Wraps each `run_tick` call in a `cycle.tick` span (attributes: active symbol count, telegram flag, rows emitted). |
| `pyproject.toml` | New optional extra `[otel]` — `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`. |

## Enabling it

Install the extra and set the flag:

```bash
uv sync --extra otel

export HUNT_OTEL=1
export HONEYCOMB_API_KEY=<your-ingest-key>   # ships spans to Honeycomb
# optional:
export HONEYCOMB_DATASET=hunter
export HUNT_ENV=prod
```

With `HONEYCOMB_API_KEY` set and no explicit endpoint, the exporter targets
Honeycomb's public OTLP/HTTP ingest (`https://api.honeycomb.io/v1/traces`) with
the `x-honeycomb-team` header. To use any other collector instead, set the
standard `OTEL_EXPORTER_OTLP_ENDPOINT` / `OTEL_EXPORTER_OTLP_HEADERS` — those take
precedence over the Honeycomb shortcut.

## Adding spans elsewhere

```python
from hunt_core.runtime import telemetry

with telemetry.span("feature.build", **{"hunt.symbol": sym}):
    vec = build_feature_vector(...)
    telemetry.set_attributes({"hunt.feature_count": len(vec)})

@telemetry.traced("scanner.prescan")
async def run_scan(...): ...
```

All helpers are safe to leave in the code permanently — they cost nothing when
`HUNT_OTEL` is unset.

## What is verified

Both code paths are covered by isolated tests:

* **Disabled (default):** `init_telemetry` returns `False`, `span()` yields `None`,
  `traced` returns the function unwrapped, `set_attributes` is a silent no-op.
* **Active:** spans are created with the expected attributes, exceptions set an
  `ERROR` status and record the exception, and nested/`traced` spans work.

The wider HUNTER stack (Python 3.14 + Binance) is not runnable in a sandbox, so
end-to-end verification against live Honeycomb data is a manual step once the
connector is authorized.

## Next step (requires your action)

The Honeycomb MCP connector needs a one-time OAuth authorization in your
claude.ai connector settings before Claude can query your traces, build boards,
or set SLOs against live data. Until then, instrumentation is emitted but the
analysis/dashboard skills can't read it back.
