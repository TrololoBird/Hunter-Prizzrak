"""Opt-in OpenTelemetry tracing for the Hunt runtime.

The whole module is gated behind the ``HUNT_OTEL`` environment variable. When it
is unset/falsey — or when the OpenTelemetry SDK is not installed — every public
helper degrades to a zero-overhead no-op. This means the instrumentation can be
sprinkled through the hot path (``with span(...)``) without changing runtime
behaviour for the default, un-instrumented deployment.

Enable it with::

    HUNT_OTEL=1
    HONEYCOMB_API_KEY=<your-ingest-key>          # Honeycomb ingest
    # or, for any OTLP collector:
    OTEL_EXPORTER_OTLP_ENDPOINT=https://collector:4317
    OTEL_EXPORTER_OTLP_HEADERS=x-custom=...       # optional

Design rules honoured (see CLAUDE.md):

* No stdlib ``logging`` in project code — diagnostics go through ``structlog``.
* Fully typed, Google-style docstrings.
* No new *hard* dependency: OpenTelemetry lives in the optional ``[otel]`` extra;
  the import is lazy and guarded, so ``import hunt_core.runtime.telemetry`` never
  fails even when the extra is absent.
"""
from __future__ import annotations

import functools
import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any, TypeVar

try:  # structlog is a core dependency, but keep telemetry importable in isolation.
    import structlog

    _LOG: Any = structlog.get_logger("hunt.telemetry")
except Exception:  # pragma: no cover - only hit in a stripped-down test env.
    import logging as _logging

    _LOG = _logging.getLogger("hunt.telemetry")

_F = TypeVar("_F", bound=Callable[..., Any])

# Module-level state, resolved once by :func:`init_telemetry`.
_ACTIVE: bool = False
_TRACER: Any = None


def _enabled_flag() -> bool:
    """Return whether ``HUNT_OTEL`` requests instrumentation.

    Returns:
        ``True`` when ``HUNT_OTEL`` is one of ``1/true/yes/on`` (case-insensitive).
    """
    return os.getenv("HUNT_OTEL", "").strip().lower() in {"1", "true", "yes", "on"}


def _build_exporter() -> Any | None:
    """Construct an OTLP span exporter from the environment.

    Honeycomb is treated as the first-class target: when ``HONEYCOMB_API_KEY`` is
    set and no explicit endpoint/headers are provided, the exporter is pointed at
    Honeycomb's public OTLP/HTTP ingest with the required ``x-honeycomb-team``
    header. Any standard ``OTEL_EXPORTER_OTLP_*`` variable still takes precedence.

    Returns:
        A configured ``OTLPSpanExporter`` instance, or ``None`` if the OpenTelemetry
        exporter package is not installed.
    """
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
    except Exception as exc:  # pragma: no cover - depends on optional extra.
        _LOG.warning("otel_exporter_unavailable", error=str(exc))
        return None

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    headers: dict[str, str] = {}
    hc_key = os.getenv("HONEYCOMB_API_KEY", "").strip()

    if hc_key and not endpoint:
        endpoint = "https://api.honeycomb.io/v1/traces"
        headers["x-honeycomb-team"] = hc_key
        dataset = os.getenv("HONEYCOMB_DATASET", "").strip()
        if dataset:
            headers["x-honeycomb-dataset"] = dataset

    if endpoint:
        return OTLPSpanExporter(endpoint=endpoint, headers=headers or None)
    return OTLPSpanExporter(headers=headers or None)


def init_telemetry(service_name: str = "hunt_core") -> bool:
    """Initialise the global tracer provider (idempotent).

    Safe to call unconditionally at startup: if ``HUNT_OTEL`` is not set, or the
    OpenTelemetry SDK is missing, it logs a single line and leaves the process in
    no-op mode.

    Args:
        service_name: Value published as the ``service.name`` resource attribute.

    Returns:
        ``True`` when tracing is live and spans will be exported, else ``False``.
    """
    global _ACTIVE, _TRACER

    if _TRACER is not None:
        return _ACTIVE
    if not _enabled_flag():
        _LOG.debug("otel_disabled")
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except Exception as exc:
        _LOG.warning(
            "otel_sdk_unavailable",
            error=str(exc),
            hint="install the optional extra: uv sync --extra otel",
        )
        return False

    exporter = _build_exporter()
    if exporter is None:
        return False

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": os.getenv("HUNT_VERSION", "1.0.0"),
            "deployment.environment": os.getenv("HUNT_ENV", "prod"),
        }
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    _TRACER = trace.get_tracer("hunt_core")
    _ACTIVE = True
    _LOG.info("otel_enabled", service=service_name)
    return True


def is_active() -> bool:
    """Return whether tracing is currently exporting spans."""
    return _ACTIVE


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """Open a span as a context manager (no-op when tracing is inactive).

    Args:
        name: Span name, e.g. ``"cycle.tick"``.
        **attributes: Initial span attributes; ``None`` values are dropped.

    Yields:
        The active span object, or ``None`` when tracing is inactive.
    """
    if not _ACTIVE or _TRACER is None:
        yield None
        return
    with _TRACER.start_as_current_span(name) as sp:
        for key, value in attributes.items():
            if value is not None:
                try:
                    sp.set_attribute(key, value)
                except Exception:  # pragma: no cover - defensive on odd types.
                    sp.set_attribute(key, repr(value))
        try:
            yield sp
        except Exception as exc:
            try:
                sp.record_exception(exc)
                from opentelemetry.trace import Status, StatusCode

                sp.set_status(Status(StatusCode.ERROR, str(exc)))
            except Exception:  # pragma: no cover
                pass
            raise


def set_attributes(mapping: Mapping[str, Any]) -> None:
    """Attach attributes to the currently-active span, if any.

    Args:
        mapping: Attribute key/value pairs; ``None`` values are skipped.
    """
    if not _ACTIVE:
        return
    try:
        from opentelemetry import trace

        sp = trace.get_current_span()
        for key, value in mapping.items():
            if value is not None:
                sp.set_attribute(key, value)
    except Exception:  # pragma: no cover
        pass


def traced(name: str | None = None) -> Callable[[_F], _F]:
    """Decorate a sync or async callable so each call opens a span.

    The wrapper detects coroutine functions and preserves async semantics. When
    tracing is inactive the original function is returned unwrapped, so there is
    no per-call overhead in the default deployment.

    Args:
        name: Span name; defaults to the function's qualified name.

    Returns:
        A decorator that wraps the target callable.
    """
    import asyncio

    def decorator(func: _F) -> _F:
        span_name: str = name or str(getattr(func, "__qualname__", getattr(func, "__name__", "call")))

        if asyncio.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                if not _ACTIVE:
                    return await func(*args, **kwargs)
                with span(span_name):
                    return await func(*args, **kwargs)

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            if not _ACTIVE:
                return func(*args, **kwargs)
            with span(span_name):
                return func(*args, **kwargs)

        return sync_wrapper  # type: ignore[return-value]

    return decorator


__all__ = [
    "init_telemetry",
    "is_active",
    "span",
    "set_attributes",
    "traced",
]
