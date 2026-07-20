"""Query plane — read materialized deep state and explain (typed :class:`NativeAnalystView`).

TG ``/signal`` uses this module. ADR-0004 Phase 9: the deep verdict is the typed native view; the
scanner ("Сканер") footer still reads the Module-2 ``hunt_scan_store`` dict. The old per-direction
blocker apparatus (``DirectionQuery``) was removed — the fusion delivery engine is gone, so it only
ever evaluated permanently-empty ``dump``/``long`` stubs (dead, G-41).
"""
from __future__ import annotations

import html
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from hunt_core.runtime.native_assembly import NativeAnalystView

STORE_FRESH_S = 180.0
STORE_STALE_S = 600.0
# Strong references to fire-and-forget background refresh tasks. asyncio keeps only a WEAK
# reference to a bare create_task result, so without this the task could be garbage-collected
# mid-flight and the store refresh silently dropped (MARKET-4). Tasks self-remove on completion.
_BG_REFRESH_TASKS: set[Any] = set()


@dataclass(frozen=True, slots=True)
class QueryResult:
    symbol: str
    native: NativeAnalystView
    source: str
    from_store: bool
    age_s: float | None
    focus_direction: Literal["short", "long"]


def row_age_seconds(row: dict[str, Any]) -> float | None:
    """Age of a Module-2 scanner dict row from its ISO ``ts`` (deep views use planes, not ``ts``)."""
    from datetime import UTC, datetime

    ts = row.get("ts")
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return (datetime.now(UTC) - dt).total_seconds()
    except (TypeError, ValueError):
        return None


def _native_age_seconds(native: NativeAnalystView) -> float | None:
    """Seconds since the typed view was built (``MarketView.now_ms`` is the tick epoch-ms)."""
    now_ms = native.view.now_ms
    if not now_ms:
        return None
    return max(0.0, (time.time() * 1000.0 - float(now_ms)) / 1000.0)


def _pick_focus(native: NativeAnalystView) -> Literal["short", "long"]:
    summary = native.prizrak.summary
    if isinstance(summary, dict):
        action = str(summary.get("action") or "")
        if action == "short":
            return "short"
        if action == "long":
            return "long"
    return "short"


def build_query_result(
    native: NativeAnalystView,
    symbol: str,
    *,
    source: str,
    from_store: bool,
    age_s: float | None,
) -> QueryResult:
    return QueryResult(
        symbol=symbol.upper(),
        native=native,
        source=source,
        from_store=from_store,
        age_s=age_s,
        focus_direction=_pick_focus(native),
    )


async def resolve_query_row(
    symbol: str,
    *,
    live: bool = False,
) -> tuple[NativeAnalystView | None, str, bool, float | None]:
    """Return ``(native, source, from_store, age_s)`` — deep store first unless ``live``.

    ADR-0004 Phase 9: the deep view is composed off the engine ``MarketRuntime``. A symbol NOT in
    the engine warm-set (or with no live price) yields ``None`` — the probe reports "not tracked"
    honestly rather than fabricating a REST snapshot (dynamic warm-set add is a later phase).
    """
    from hunt_core.maps.engine import get_map_store
    from hunt_core.runtime.analyst_assembly import assemble_analyst_tick
    from hunt_core.runtime.symbol_probe import normalize_symbol
    from hunt_core.runtime.tick_state import deep_query_store, live_market_runtime

    sym = normalize_symbol(symbol)
    if not live:
        cached = deep_query_store().get(sym)
        if cached is not None:
            age_s = _native_age_seconds(cached)
            if age_s is None or age_s <= STORE_FRESH_S:
                return cached, "deep_store", True, age_s

    rt = live_market_runtime()
    if rt is None:
        return None, "engine_unavailable", False, None
    native = await assemble_analyst_tick(sym, rt, store=get_map_store())
    return native, ("deep_live" if live else "analyst_assembly"), False, None


def format_query_telegram(q: QueryResult, *, added_watch: bool = False) -> str:
    """Analyst-first /signal — analysis/structure/MTF/maps; hunt scan collapsed footer."""
    from hunt_core.prizrak.build import build_deep_report as _build_deep_report
    from hunt_core.prizrak.format_telegram import format_deep_analysis_telegram as _fmt_deep
    from hunt_core.runtime.tick_state import hunt_scan_store

    try:
        analysis = _build_deep_report(q.native, include_watch_appendix=False)
    except Exception:
        # Fail-loud: surface the real traceback to logs instead of swallowing it. User still gets
        # a graceful card. Every section below dereferences `analysis`, so the fallback MUST return
        # here — falling through raised NameError on exactly the path this handler exists for.
        import structlog

        structlog.get_logger("hunt_core.runtime.query_service").exception(
            "report_build_failed", symbol=q.symbol, from_store=q.from_store
        )
        return (
            f"🔬 <b>Глубокий анализ — {html.escape(q.symbol)}</b>\n"
            "<i>анализ временно недоступен · /signal SYM --live для REST</i>"
        )

    parts: list[str] = [_fmt_deep(analysis)]

    from hunt_core.data.universe import is_pinned_symbol

    prizrak_action = str((q.native.prizrak.summary or {}).get("action") or "").upper()

    # When analyst says WAIT, show the opposite direction's scanner read too.
    if prizrak_action in {"WAIT", ""}:
        opposite_dir = "long" if q.focus_direction == "short" else "short"
        dir_ru = "ЛОНГ" if opposite_dir == "long" else "ШОРТ"
        scan_row = hunt_scan_store().get(q.symbol)
        scan_has_setup = False
        if isinstance(scan_row, dict):
            scan_setup = scan_row.get("dump" if opposite_dir == "short" else "long") or {}
            scan_has_setup = bool(scan_setup.get("impulse_confirmed"))
        alt_label = "сетап в сканере (раннее обнаружение)" if scan_has_setup else "нет сетапа"
        if alt_label != "нет сетапа":
            parts.extend(["", "—", f"🔄 <b>Альтернатива: {dir_ru}</b> — {alt_label}"])

    # Scanner section (always shown; independent from analyst verdict).
    if not is_pinned_symbol(q.symbol):
        watch_lines: list[str] = []
        if added_watch:
            watch_lines.append("<i>+ watchlist</i>")
        hunt_row = hunt_scan_store().get(q.symbol)
        if isinstance(hunt_row, dict) and not hunt_row.get("error"):
            has_short = bool(hunt_row.get("dump", {}).get("impulse_confirmed"))
            has_long = bool(hunt_row.get("long", {}).get("impulse_confirmed"))
            scan_texts = []
            if has_short:
                scan_texts.append("🔴 сканер: шорт-сетап есть")
            if has_long:
                scan_texts.append("🟢 сканер: лонг-сетап есть")
            if not scan_texts:
                phase_s = str(hunt_row.get("dump", {}).get("phase") or "—")
                phase_l = str(hunt_row.get("long", {}).get("phase") or "—")
                scan_texts.append(f"сканер: шорт={phase_s} · лонг={phase_l}")
            watch_lines.append(f"<i>{' · '.join(scan_texts)}</i>")
        else:
            watch_lines.append("<i>сканер: нет данных по символу</i>")
        if watch_lines:
            parts.extend(["", "—", "<b>Сканер</b> (модуль раннего обнаружения)", *watch_lines])

    return "\n".join(parts)


def format_freshness_footer(q: QueryResult) -> str:
    """Source/freshness tag — always the LAST line of the full reply (after any level-map/DOM
    sections appended by the caller), never in the middle."""
    if q.from_store:
        as_of = (q.native.freshness or {}).get("as_of")
        as_of_txt = ""
        if as_of:
            as_of_txt = f" · снимок {html.escape(str(as_of)[:19].replace('T', ' '))} UTC"
        age_txt = f"{q.age_s:.0f}s назад" if q.age_s is not None else "watch-тик"
        return (
            f"\n<i>📊 из кэша ({age_txt}{as_of_txt}) · "
            f"/signal {q.symbol.replace('USDT', '')} --live для REST</i>"
        )
    return format_row_freshness_footer(q.native, source=q.source)


def format_row_freshness_footer(native: NativeAnalystView, *, source: str) -> str:
    """Source/as-of stamp for a typed deep view (no QueryResult in hand).

    Used by the pinned push path. The stamp matters because ``TelegramBroadcaster`` buffers
    messages while its circuit is open and replays them later — without an as-of line a stale card
    is indistinguishable from a live one.

    Args:
        native: The typed deep view (reads ``freshness.as_of`` + ``view.now_ms``).
        source: Short provenance tag rendered after the satellite glyph.

    Returns:
        A leading-newline ``<i>…</i>`` footer line, appended last by the caller.
    """
    as_of = (native.freshness or {}).get("as_of")
    tail = f" · {html.escape(str(as_of)[:19].replace('T', ' '))} UTC" if as_of else ""
    age = _native_age_seconds(native)
    age_txt = f" · тик {age:.0f}s назад" if age is not None else ""
    return f"\n<i>🛰 {html.escape(source)}{tail}{age_txt}</i>"


def spawn_background_refresh(symbol: str) -> None:
    """Non-blocking deep refresh after a stale store hit — updates the deep store only."""
    import asyncio

    from hunt_core.runtime.tick_state import deep_query_store

    async def _run() -> None:
        try:
            native, _src, _store, _age = await resolve_query_row(symbol, live=True)
            if native is not None:
                deep_query_store().put(symbol.upper(), native)
        except Exception:
            import structlog

            structlog.get_logger("hunt_core.runtime.query_service").debug(
                "background_refresh_failed", symbol=symbol, exc_info=True
            )

    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(_run())
        _BG_REFRESH_TASKS.add(task)
        task.add_done_callback(_BG_REFRESH_TASKS.discard)
    except RuntimeError:
        pass


__all__ = [
    "QueryResult",
    "STORE_FRESH_S",
    "STORE_STALE_S",
    "build_query_result",
    "format_freshness_footer",
    "format_query_telegram",
    "format_row_freshness_footer",
    "resolve_query_row",
    "row_age_seconds",
    "spawn_background_refresh",
]
