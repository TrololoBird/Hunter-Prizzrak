"""Query plane — read materialized watch state and explain (QueryResult ≠ DeliveryGate).

TG ``/signal`` and ``_dev/probe_delivery`` use this module. Delivery still runs
``evaluate_delivery*`` only to answer *would deliver now*; blockers are always listed.
"""
from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from hunt_core.scanner.detect.delivery_support import GateResult
from hunt_core.market.client import HuntCcxtClient

STORE_FRESH_S = 180.0
STORE_STALE_S = 600.0
_HOT_TICK_PATHS = frozenset({"hot_ws", "hot_bootstrap", "hot_delta", "hot_carry"})
_MAX_BLOCKERS_SHOWN = 5


@dataclass(frozen=True, slots=True)
class DirectionQuery:
    direction: Literal["short", "long"]
    confirmed: bool
    formation: GateResult
    blockers: tuple[GateResult, ...]
    delivery_gate: GateResult | None
    delivery_tier: Any | None
    would_deliver: bool


@dataclass(frozen=True, slots=True)
class QueryResult:
    symbol: str
    row: dict[str, Any]
    source: str
    from_store: bool
    age_s: float | None
    short: DirectionQuery
    long: DirectionQuery
    focus_direction: Literal["short", "long"]

    def focus(self) -> DirectionQuery:
        return self.short if self.focus_direction == "short" else self.long


def row_age_seconds(row: dict[str, Any]) -> float | None:
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


def _pick_focus(row: dict[str, Any]) -> Literal["short", "long"]:
    summary = row.get("prizrak_summary")
    if isinstance(summary, dict):
        action = str(summary.get("action") or "")
        if action == "short":
            return "short"
        if action == "long":
            return "long"
    dump = row.get("dump") or {}
    long_setup = row.get("long") or {}
    if dump.get("impulse_confirmed") and not long_setup.get("impulse_confirmed"):
        return "short"
    if long_setup.get("impulse_confirmed") and not dump.get("impulse_confirmed"):
        return "long"
    return "short"


def _dedupe_blockers(blockers: list[GateResult]) -> list[GateResult]:
    seen: set[str] = set()
    out: list[GateResult] = []
    for item in blockers:
        if item.code in seen:
            continue
        seen.add(item.code)
        out.append(item)
    return out


def _evaluate_direction(
    row: dict[str, Any],
    *,
    direction: Literal["short", "long"],
    symbol: str,
    lc: dict[str, Any],
    from_store: bool,
    sniper_config: Any,
) -> DirectionQuery:
    from hunt_core.scanner.detect.delivery_support import collect_report_blockers, evaluate_formation

    # row["dump"]/row["long"] are permanently neutral stubs (impulse_confirmed
    # always False) since the fusion detection engine was removed —
    # manipulation.py is the only real Hunter signal source now and never
    # populates these keys, so there is no delivery gate left to evaluate here.
    setup = (row.get("dump") if direction == "short" else row.get("long")) or {}
    confirmed = False
    formation = evaluate_formation(
        setup, direction=direction, symbol=symbol, lifecycle=lc
    )
    blockers = _dedupe_blockers(
        collect_report_blockers(
            setup,
            direction=direction,
            symbol=symbol,
            lifecycle=lc,
            row=row,
            sniper_config=sniper_config,
            fast_lane=from_store,
        )
    )
    delivery_gate: GateResult | None = None
    delivery_tier: Any | None = None
    would_deliver = False
    return DirectionQuery(
        direction=direction,
        confirmed=confirmed,
        formation=formation,
        blockers=tuple(blockers),
        delivery_gate=delivery_gate,
        delivery_tier=delivery_tier,
        would_deliver=would_deliver,
    )


def build_query_result(
    row: dict[str, Any],
    symbol: str,
    *,
    source: str,
    from_store: bool,
    age_s: float | None,
    sniper_config: Any = None,
) -> QueryResult:
    sym = symbol.upper()
    lc = row.get("lifecycle") if isinstance(row.get("lifecycle"), dict) else {}
    if sniper_config is None:
        from hunt_core.runtime.state import SNIPER_CONFIG

        sniper_config = SNIPER_CONFIG
    short_q = _evaluate_direction(
        row, direction="short", symbol=sym, lc=lc, from_store=from_store, sniper_config=sniper_config
    )
    long_q = _evaluate_direction(
        row, direction="long", symbol=sym, lc=lc, from_store=from_store, sniper_config=sniper_config
    )
    return QueryResult(
        symbol=sym,
        row=row,
        source=source,
        from_store=from_store,
        age_s=age_s,
        short=short_q,
        long=long_q,
        focus_direction=_pick_focus(row),
    )


async def resolve_query_row(
    symbol: str,
    *,
    live: bool = False,
    stagger_ms: int = 200,
    client: HuntCcxtClient | None = None,
) -> tuple[dict[str, Any], str, bool, float | None]:
    """Return ``(row, source, from_store, age_s)`` — DeepQueryStore first unless ``live``."""
    from hunt_core.runtime.analyst_assembly import assemble_analyst_tick
    from hunt_core.runtime.symbol_probe import normalize_symbol
    from hunt_core.runtime.tick_state import deep_query_store

    sym = normalize_symbol(symbol)
    row: dict[str, Any] | None = None
    from_store = False
    age_s: float | None = None
    source = "live_rest"

    if live:
        row = await assemble_analyst_tick(sym, client, stagger_ms=max(stagger_ms, 200))
        source = "deep_live"
    else:
        cached = deep_query_store().resolve(sym)
        if isinstance(cached, dict) and not cached.get("error"):
            age_s = row_age_seconds(cached)
            if age_s is None or age_s <= STORE_FRESH_S:
                row = cached
                from_store = True
                source = str(cached.get("tick_path") or "deep_store")
            elif age_s is not None and age_s <= STORE_STALE_S:
                # Between STORE_FRESH_S and STORE_STALE_S: force a fresh REST probe
                # instead of sending stale data with an apology.
                row = None

    if row is None:
        row = await assemble_analyst_tick(sym, client, stagger_ms=max(stagger_ms, 200))
        source = "analyst_assembly"

    if row.get("maps") and not row.get("maps_forecast"):
        from hunt_core.toolkit.forecast import stamp_forecasts_on_row

        stamp_forecasts_on_row(row)

    return row, source, from_store, age_s


def _format_blockers_section(dq: DirectionQuery) -> list[str]:
    lines: list[str] = []

    if dq.confirmed:
        dir_ru = "ШОРТ" if dq.direction == "short" else "ЛОНГ"
        if dq.would_deliver:
            tier = getattr(dq.delivery_tier, "tier", None) or (
                dq.delivery_tier.get("tier")
                if isinstance(dq.delivery_tier, dict)
                else None
            )
            tier_txt = f" · tier <code>{html.escape(str(tier))}</code>" if tier else ""
            lines.append(f"✅ Delivery {dir_ru}: прошёл бы{tier_txt}")
        elif dq.delivery_gate is not None:
            g = dq.delivery_gate
            lines.append(
                f"🚫 Delivery {dir_ru}: "
                f"<code>{html.escape(g.code or 'gate')}</code>"
            )
    return lines


def format_query_telegram(q: QueryResult, *, added_watch: bool = False) -> str:
    """Analyst-first /signal — analysis/structure/MTF/maps; hunt scan collapsed footer."""
    from hunt_core.prizrak.build import build_deep_report as _build_deep_report
    from hunt_core.prizrak.format_telegram import format_deep_analysis_telegram as _fmt_deep
    from hunt_core.runtime.tick_state import hunt_scan_store

    try:
        analysis = _build_deep_report(q.row, include_watch_appendix=False)
        parts: list[str] = [_fmt_deep(analysis)]
    except Exception:
        # Fail-loud: surface the real traceback to logs instead of swallowing it.
        # User still gets a graceful card.
        import structlog

        structlog.get_logger("hunt_core.runtime.query_service").exception(
            "report_build_failed", symbol=q.symbol, from_store=q.from_store
        )
        parts = [
            f"🔬 <b>Глубокий анализ — {html.escape(q.symbol)}</b>\n"
            "<i>анализ временно недоступен · /signal SYM --live для REST</i>"
        ]

    from hunt_core.data.universe import is_pinned_symbol

    prizrak_action = str((analysis.row.get("prizrak_summary") or {}).get("action") or "").upper()

    # When analyst says WAIT, show opposite direction too
    if prizrak_action in {"WAIT", ""}:
        from hunt_core.runtime.tick_state import hunt_scan_store
        opposite_dir = "long" if q.focus_direction == "short" else "short"
        dir_ru = "ЛОНГ" if opposite_dir == "long" else "ШОРТ"
        opposite_q = q.long if opposite_dir == "long" else q.short
        scan_row = hunt_scan_store().get(q.symbol)
        scan_has_setup = False
        if isinstance(scan_row, dict):
            scan_setup = scan_row.get("dump" if opposite_dir == "short" else "long") or {}
            scan_has_setup = bool(scan_setup.get("impulse_confirmed") or scan_setup.get("intrabar_confirmed"))
        if opposite_q.would_deliver:
            alt_label = "можно войти"
        elif opposite_q.confirmed:
            alt_label = "сетап есть"
        elif scan_has_setup:
            alt_label = "сетап в сканере (раннее обнаружение)"
        else:
            alt_label = "нет сетапа"
        opp_lines = _format_blockers_section(opposite_q)
        if opp_lines:
            parts.extend(["", "—", f"🔄 <b>Альтернатива: {dir_ru}</b> — {alt_label}", *opp_lines])
        elif alt_label != "нет сетапа":
            parts.extend(["", "—", f"🔄 <b>Альтернатива: {dir_ru}</b> — {alt_label}"])

    # Scanner section (always shown; independent from analyst verdict)
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
    """Source/freshness tag — always the LAST line of the full reply (after any
    level-map/DOM/liquidation sections appended by the caller), never in the middle.
    A mid-message "🛰 source · timestamp" line visually reads as a second message's
    header, which is confusing — this must be appended last, once, by the caller.
    """
    if q.from_store:
        as_of = q.row.get("as_of") or (q.row.get("freshness") or {}).get("as_of")
        as_of_txt = ""
        if as_of:
            as_of_txt = f" · снимок {html.escape(str(as_of)[:19].replace('T', ' '))} UTC"
        age_txt = f"{q.age_s:.0f}s назад" if q.age_s is not None else "watch-тик"
        return (
            f"\n<i>📊 из кэша ({age_txt}{as_of_txt}) · "
            f"/signal {q.symbol.replace('USDT', '')} --live для REST</i>"
        )
    as_of = q.row.get("as_of")
    tail = f" · {html.escape(str(as_of)[:19].replace('T', ' '))} UTC" if as_of else ""
    return f"\n<i>🛰 {html.escape(q.source)}{tail}</i>"


def spawn_background_refresh(
    symbol: str,
    *,
    client: HuntCcxtClient | None = None,
    stagger_ms: int = 200,
) -> None:
    """Non-blocking REST refresh after a stale store hit — updates LastTickStore only."""
    import asyncio

    from hunt_core.runtime.tick_state import deep_query_store

    async def _run() -> None:
        try:
            row, _src, _store, _age = await resolve_query_row(
                symbol, live=True, stagger_ms=stagger_ms, client=client
            )
            if isinstance(row, dict) and not row.get("error"):
                deep_query_store().put(symbol.upper(), row)
        except Exception:
            import structlog

            structlog.get_logger("hunt_core.runtime.query_service").debug(
                "background_refresh_failed", symbol=symbol, exc_info=True
            )

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_run())
    except RuntimeError:
        pass


__all__ = [
    "DirectionQuery",
    "QueryResult",
    "STORE_FRESH_S",
    "build_query_result",
    "format_freshness_footer",
    "format_query_telegram",
    "resolve_query_row",
    "row_age_seconds",
    "spawn_background_refresh",
]
