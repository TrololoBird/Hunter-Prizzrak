"""Add repo root + package parent to sys.path; verify Polars feature stack."""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

_FEATURE_STACK: tuple[str, ...] = (
    "polars",
    "polars_ta",
    "polars_ols",
    "polars_ds",
    # polars-trading is optional (fallbacks in research_plugins.py)
)


def _patch_aiohttp_resolver() -> None:
    """Replace pycares AsyncResolver with ThreadedResolver to prevent DNS hangs.

    pycares has a known deadlock on macOS + Python 3.14 where
    _run_safe_shutdown_loop blocks forever, freezing all aiohttp
    connections (Telegram, CCXT REST/WS).  ThreadedResolver avoids
    pycares entirely by using the OS thread pool.
    """
    try:
        import aiohttp.resolver

        if aiohttp.resolver.DefaultResolver is aiohttp.resolver.AsyncResolver:
            aiohttp.resolver.DefaultResolver = aiohttp.resolver.ThreadedResolver
            setattr(aiohttp.resolver, "AsyncResolver", aiohttp.resolver.ThreadedResolver)
    except (ImportError, AttributeError):
        pass


def bootstrap() -> Path:
    hunt_root = Path(__file__).resolve().parents[1]
    repo = hunt_root.parent
    for p in (str(repo), str(hunt_root)):
        if p not in sys.path:
            sys.path.insert(0, p)
    os.environ.setdefault("POLARS_STREAMING", "1")
    _patch_aiohttp_resolver()
    _init_telemetry()
    return repo


def _init_telemetry() -> None:
    """Initialise opt-in OpenTelemetry tracing (no-op unless ``HUNT_OTEL`` is set).

    Kept defensive: telemetry must never be able to break process startup.
    """
    try:
        from hunt_core.runtime.telemetry import init_telemetry

        init_telemetry("hunt_core")
    except Exception:
        pass


def require_feature_stack() -> None:
    """Fail fast when core Polars TA dependencies are missing."""
    missing: list[str] = []
    for mod in _FEATURE_STACK:
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        raise ImportError(
            "Hunt requires Polars feature stack: "
            f"{', '.join(missing)}. Install: uv sync --all-extras"
        )


__all__ = ["bootstrap", "require_feature_stack"]
