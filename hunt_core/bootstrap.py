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
    # ⚠ `polars_trading` здесь не значится не потому, что «опционален» — так гласил
    # прежний комментарий, — а потому, что 2026-08-03 удалён совсем. Пакет не был объявлен
    # ни в pyproject, ни в uv.lock и не стоял в окружении, то есть «фоллбэк» в
    # research_plugins.py был единственным исполнявшимся путём с самого начала.
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

    Kept defensive: telemetry must never be able to break process startup — но отказ
    обязан быть слышен. Прежняя редакция глотала его в ``pass``: телеметрия молча не
    поднималась, и узнать об этом было нечем, кроме пустых трасс. Директива владельца
    2026-07-31 запрещает ровно это («деградации НЕ ДОПУСТИМЫ» — молча).

    Печать идёт в ``stderr``, а не в ``structlog``: точка вызова — ``bootstrap``, до
    настройки логирования, и обращение к логгеру здесь само может бросить.
    """
    try:
        from hunt_core.runtime.telemetry import init_telemetry

        init_telemetry("hunt_core")
    except Exception as exc:  # noqa: BLE001 — старт процесса важнее телеметрии
        print(f"[bootstrap] телеметрия не поднялась, трассы будут пусты: {exc!r}", file=sys.stderr)


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
