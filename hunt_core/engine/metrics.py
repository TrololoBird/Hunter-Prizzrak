"""Prometheus instrumentation for the engine (library-adoption.md #1 — the silent-blackout guard).

The project's worst recurring failure class is a *silent* data blackout: the WS feed freezes,
ccxt reports ``errors=0``, and it is discovered hours later via ``ps`` (memories
``stale-htf-cache-trap``, ``pinned-4h-stale-blackout``, ``live-crash-proxy``). A
``feed_silence_seconds`` gauge is a fail-loud instrument *by construction* — when data stops it
climbs unbounded and becomes alertable in seconds.

Cardinality discipline (the one prometheus footgun): label by **venue** and **plane TYPE** only,
NEVER per-symbol — a per-symbol label on a 100-symbol universe explodes the series count.
Emit-only; a scraper/alert rule on ``feed_silence_seconds > NO_MESSAGE_WATCHDOG_S`` closes the loop.
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge

FEED_SILENCE = Gauge(
    "hunter_engine_feed_silence_seconds",
    "Seconds since the newest frame across the whole feed (climbs unbounded on a silent blackout).",
    ["venue"],
)
WS_RECONNECTS = Counter(
    "hunter_engine_ws_reconnects_total",
    "Forced WS reconnects (watchdog silence / scheduled 24h rotate).",
    ["venue", "reason"],
)
STALENESS_REJECTS = Counter(
    "hunter_engine_staleness_rejects_total",
    "Snapshot plane reads rejected as stale (fail-loud NotReady), by plane type.",
    ["plane"],
)
HEALTHY_SYMBOLS = Gauge(
    "hunter_engine_healthy_symbols",
    "Symbols whose last required snapshot was fully ready (no absent/stale plane).",
    ["venue"],
)


def _plane_type(plane: str) -> str:
    """Coarsen a plane name to its low-cardinality type (``kline.4h`` → ``kline``)."""
    return plane.split(".", 1)[0] if plane else "unknown"


def set_feed_silence(venue: str, seconds: float) -> None:
    FEED_SILENCE.labels(venue=venue).set(seconds)


def record_reconnect(venue: str, reason: str) -> None:
    WS_RECONNECTS.labels(venue=venue, reason=reason).inc()


def record_staleness_reject(plane: str) -> None:
    STALENESS_REJECTS.labels(plane=_plane_type(plane)).inc()


def set_healthy_symbols(venue: str, count: int) -> None:
    HEALTHY_SYMBOLS.labels(venue=venue).set(count)


__all__ = [
    "FEED_SILENCE",
    "WS_RECONNECTS",
    "STALENESS_REJECTS",
    "HEALTHY_SYMBOLS",
    "set_feed_silence",
    "record_reconnect",
    "record_staleness_reject",
    "set_healthy_symbols",
]
