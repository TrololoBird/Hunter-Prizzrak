"""Per-tick watch loop — typed native assembly, tracking refresh, follow-ups (ADR-0004 Phase 9).

The main tick composes the typed :class:`~hunt_core.runtime.native_assembly.NativeAnalystView` per
active symbol (``assemble_native_analyst``) and drives every consumer off those handles — there is no
``snapshot_symbol`` row dict here any more. The tick is NOT an emission surface: the deep loop emits
Prizrak signals; this tick refreshes open-signal tracking (SL/TP follow-ups, kline/ticker reconcile),
persists one scan row per symbol, and updates ``prev_oi``. The persisted scan dict is a disk
serializer (allowed), built from the typed view — never routed back into in-memory logic.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from hunt_core import clock
from hunt_core.confluence.mtf import build_mtf_confluence_native, mtf_confluence_to_dict
from hunt_core.data.lake import (
    FeatureLakeWriter,
    buffer_tracker_state,
    flush_lake,
)
from hunt_core.data.tick_jsonl import ensure_fusion_lifecycle_fields
from hunt_core.data.universe import PINNED_SYMBOLS
from hunt_core.deliver.digest import get_advisory_digest
from hunt_core.deliver.telegram import TelegramBroadcaster
from hunt_core.errors import defensive_exc_types
from hunt_core.features.feature_engine import (
    FeatureExtractError,
    build_feature_vector_native,
)
from hunt_core.maps.engine import MapTimeSeriesStore, derive_map_features
from hunt_core.runtime.cycle._cycle_reconcile import (
    _deliver_followup,
    _reconcile_inwatch_active,
    _reconcile_orphan_signals,
    _record_followup_side_effects,
)
from hunt_core.runtime.native_assembly import NativeAnalystView, assemble_native_analyst
from hunt_core.runtime.state import LOG, SymbolStateStore, WatchMode
from hunt_core.track.tracker import (
    auto_resolve_active_signals,
    evaluate_followups,
    iter_active_tracker_symbols,
    latch_row_setups,
    load_tracker_state,
    reconcile_active_from_ticker,
)
from hunt_core.view.runtime import MarketRuntime


def _compact(symbol: str) -> str:
    """Unified ``BTC/USDT:USDT`` → compact ``BTCUSDT`` (tracker / prev_oi / store key form)."""
    return symbol.split(":", 1)[0].replace("/", "").upper()


def _unified(compact: str) -> str:
    """Compact ``BTCUSDT`` → ccxt-unified ``BTC/USDT:USDT`` for the engine view assembly."""
    base = compact.upper().replace("/", "").replace(":USDT", "")
    if base.endswith("USDT"):
        base = base[:-4]
    return f"{base}/USDT:USDT"


def _serialize_native_scan_row(
    nav: NativeAnalystView,
    *,
    dump: dict[str, Any],
    long: dict[str, Any],
    lifecycle: dict[str, Any],
    mtf_dict: dict[str, Any] | None,
) -> dict[str, Any]:
    """Project the typed view onto the persisted Module-2 scan row (disk format, not a bridge).

    Emits exactly the keys the persistence consumers read back off ``hunt_scan_store`` / the tick
    JSONL / diagnostics: ``symbol`` / ``ts`` / ``price`` / ``plane`` for identity + freshness, the
    (latched) neutral ``dump`` / ``long`` setup stubs + normalized ``lifecycle`` + ``mtf`` for the
    ``/signal`` scanner footer and replay gates, and the map/regime/session context for diagnostics.
    NOT a legacy-row reconstruction: no ``timeframes`` / ``structure`` / positioning god-object keys.
    """
    compact = _compact(nav.view.symbol)
    market = (
        derive_map_features(nav.maps, current_price=nav.view.last_price)
        if nav.maps is not None
        else {}
    )
    return {
        "symbol": compact,
        "ts": nav.freshness.get("as_of"),
        "price": nav.view.last_price,
        "plane": "hunt",
        "tick_path": "native_assembly",
        "dump": dump,
        "long": long,
        "lifecycle": lifecycle,
        "market": market,
        "regime": nav.features.regime.model_dump(),
        "session": nav.session or {},
        "freshness": nav.freshness,
        "mtf": mtf_dict,
    }


async def run_tick(
    symbols: tuple[str, ...],
    *,
    settings: Any,
    rt: MarketRuntime,
    store: MapTimeSeriesStore,
    prev_oi: dict[str, float | None],
    last_bias: dict[str, str],
    last_lifecycle_phase: dict[str, str],
    mode_map: dict[str, WatchMode],
    broadcaster: TelegramBroadcaster | None,
    send_telegram: bool,
    ticker_by_sym: dict[str, dict[str, Any]] | None = None,
    pump_store: Any | None = None,
    cross_ex_cache: dict[str, dict[str, Any]] | None = None,
    prescan_outlier_by_sym: dict[str, dict[str, Any]] | None = None,
    symbol_state: SymbolStateStore | None = None,
    feature_lake: FeatureLakeWriter | None = None,
) -> list[dict[str, Any]]:
    """Assemble the typed native view per active symbol and refresh open-signal tracking.

    Args:
        symbols: Active-subset compact symbols (``BTCUSDT``) this tick enriches.
        rt: The engine-native runtime — source of every :class:`MarketView`.
        store: The map time-series store threaded into ``assemble_native_analyst``.
        prev_oi: Compact-keyed OI carry (updated from ``view.derivs.oi``).
        ticker_by_sym: 24h ticker rows for symbols rotated out of this tick (reconcile safety net).
        Other args: lifecycle/telemetry carries + the feature-lake writer.

    Returns:
        The per-symbol persisted scan rows (also pushed to ``hunt_scan_store``).
    """
    _ = (last_bias, last_lifecycle_phase, mode_map, cross_ex_cache, symbol_state, settings)
    from hunt_core.runtime.cycle import _impl as _tick_impl

    _load_state = _tick_impl._load_state
    _save_state = _tick_impl._save_state
    parallel = max(1, int(_tick_impl.HUNT_SNAPSHOT_PARALLEL))
    symbol_timeout_s = _tick_impl.SYMBOL_TICK_TIMEOUT_S

    state = _load_state()
    tracker_state = load_tracker_state()
    now = clock.now_utc()
    exchange = rt.multi.primary.exchange
    rows: list[dict[str, Any]] = []
    _tick_success = False
    try:
        ordered = list(symbols)
        tick_started = time.monotonic()

        async def _native_one(
            sym: str,
        ) -> tuple[str, dict[str, Any] | None, NativeAnalystView | None]:
            try:
                nav = await asyncio.wait_for(
                    assemble_native_analyst(rt, _unified(sym), store=store),
                    timeout=symbol_timeout_s,
                )
            except TimeoutError:
                LOG.warning("watch_symbol_timeout", symbol=sym, timeout_s=symbol_timeout_s)
                return sym, _error_row(sym, "symbol_tick_timeout", now), None
            except defensive_exc_types(asyncio.IncompleteReadError) as exc:
                LOG.warning("dump_symbol_failed", symbol=sym, error=repr(exc))
                return sym, _error_row(sym, repr(exc), now), None
            if nav is None:
                return sym, _error_row(sym, "not_ready", now), None
            return sym, None, nav

        sem = asyncio.Semaphore(parallel)

        async def _bounded(
            sym: str,
        ) -> tuple[str, dict[str, Any] | None, NativeAnalystView | None]:
            async with sem:
                return await _native_one(sym)

        # return_exceptions=True so ONE symbol raising outside the caught set does not sink the whole
        # tick — every other symbol's already-assembled view would otherwise be discarded.
        raw_results = await asyncio.gather(
            *[_bounded(s) for s in ordered], return_exceptions=True
        )
        settled = _settle_native_results(ordered, raw_results, now=now)

        snap_elapsed = round(time.monotonic() - tick_started, 2)
        if len(ordered) > 1:
            ready_n = sum(1 for s in ordered if settled.get(s, (None, None))[1] is not None)
            LOG.info(
                "watch_snapshot_batch",
                symbols=len(ordered),
                parallel=parallel,
                elapsed_s=snap_elapsed,
                ready=ready_n,
                not_ready=len(ordered) - ready_n,
            )

        for symbol in ordered:
            err_row, nav = settled.get(symbol, (None, None))
            try:
                if nav is None:
                    if err_row is not None:
                        LOG.info(
                            "watch_symbol_data_reject",
                            symbol=symbol,
                            error=err_row.get("error"),
                        )
                        rows.append(err_row)
                    continue
                compact = _compact(nav.view.symbol)

                # One lake row per CLOSED 15m bar (typed close_time_ms, no -2 frame read) — keeps the
                # fusion reference distribution causal and un-oversampled (Phase 0.1).
                if feature_lake is not None and compact not in PINNED_SYMBOLS:
                    tf15 = nav.features.tf.get("15m")
                    close_time = tf15.close_time_ms if tf15 is not None else None
                    lake_key = f"{compact}:lake_bar"
                    if close_time is not None and state.get(lake_key) != str(close_time):
                        as_of = str(nav.freshness.get("as_of"))
                        try:
                            market_feats = (
                                derive_map_features(
                                    nav.maps, current_price=nav.view.last_price
                                )
                                if nav.maps is not None
                                else {}
                            )
                            vector = build_feature_vector_native(
                                nav.view,
                                nav.features,
                                tf="15m",
                                ts=as_of,
                                market_features=market_feats,
                                session=nav.session,
                            )
                            feature_lake.enqueue(compact, as_of, "15m", vector.to_dict())
                            state[lake_key] = str(close_time)
                        except FeatureExtractError as exc:
                            LOG.warning(
                                "feature_lake_enqueue_skipped", symbol=compact, error=str(exc)
                            )

                oi_val = nav.view.derivs.oi
                if oi_val is not None:
                    prev_oi[compact] = float(oi_val)

                # The tick never emits — neutral setup stubs + neutral lifecycle. ``latch_row_setups``
                # reflects any OPEN TG-sent signal back onto the matching side for the scanner footer.
                dump_stub, long_stub = latch_row_setups(
                    tracker_state, symbol=compact, dump={}, long={}
                )
                neutral_lc = ensure_fusion_lifecycle_fields(
                    {"phase": "neutral", "phase_fusion": "neutral"}
                )
                mtf = build_mtf_confluence_native(nav.view, nav.features)
                persist = _serialize_native_scan_row(
                    nav,
                    dump=dump_stub,
                    long=long_stub,
                    lifecycle=neutral_lc,
                    mtf_dict=mtf_confluence_to_dict(mtf) if mtf is not None else None,
                )
                if prescan_outlier_by_sym and compact in prescan_outlier_by_sym:
                    persist["prescan_outlier"] = prescan_outlier_by_sym[compact]
                rows.append(persist)

                kline_events = await _reconcile_inwatch_active(
                    exchange, tracker_state, symbol=compact, now=now
                )
                followup_sent_keys: set[str] = set()
                for fu in kline_events:
                    LOG.info(
                        "watch_followup_kline",
                        symbol=fu.symbol,
                        followup_event=fu.event,
                        detail=fu.detail,
                    )
                    if await _deliver_followup(
                        broadcaster,
                        fu,
                        persist,
                        tracker_state,
                        now=now,
                        send_telegram=send_telegram,
                    ):
                        followup_sent_keys.add(fu.message_key)
                followups = evaluate_followups(
                    tracker_state,
                    view=nav.view,
                    features=nav.features,
                    maps=nav.maps,
                    session=nav.session,
                    lifecycle=neutral_lc,
                    now=now,
                )
                for fu in followups:
                    if fu.message_key in followup_sent_keys:
                        continue
                    LOG.info(
                        "watch_followup",
                        symbol=fu.symbol,
                        followup_event=fu.event,
                        detail=fu.detail,
                    )
                    if await _deliver_followup(
                        broadcaster,
                        fu,
                        persist,
                        tracker_state,
                        now=now,
                        send_telegram=send_telegram,
                    ):
                        followup_sent_keys.add(fu.message_key)
                if followup_sent_keys:
                    _record_followup_side_effects(
                        [*kline_events, *followups],
                        sent_keys=followup_sent_keys,
                        now=now,
                        pump_store=pump_store,
                    )
            except Exception:
                LOG.exception("watch_symbol_process_failed", symbol=symbol)

        # Ticker safety net: symbols rotated out of this tick's batch still get SL/TP extremes from
        # the already-fetched 24h ticker (universe rotation gap — MEGA @ SL while last_checked froze).
        seen = set(symbols)
        active_syms = {sym for sym, _ in iter_active_tracker_symbols(tracker_state)}
        missing_active = active_syms - seen
        ticker_events: list[Any] = []
        if missing_active and ticker_by_sym:
            ticker_now = clock.now_utc()
            ticker_events = reconcile_active_from_ticker(
                tracker_state,
                ticker_by_sym=ticker_by_sym,
                now=ticker_now,
                only_symbols=missing_active,
            )
            if ticker_events:
                LOG.info(
                    "watch_ticker_reconcile",
                    symbols=sorted(missing_active),
                    events=len(ticker_events),
                )

        # Orphan reconciliation: active signals whose symbol left the watchlist would otherwise never
        # close (PLAYUSDT held TP2 for 18h unnoticed). Engine REST tail, not the legacy client.
        orphan_events = await _reconcile_orphan_signals(
            exchange, tracker_state, seen_symbols=seen, now=clock.now_utc()
        )
        orphan_events = ticker_events + orphan_events
        if orphan_events:
            orphan_now = clock.now_utc()
            orphan_sent: set[str] = set()
            for fu in orphan_events:
                LOG.info(
                    "watch_followup_orphan",
                    symbol=fu.symbol,
                    followup_event=fu.event,
                    detail=fu.detail,
                )
                if await _deliver_followup(
                    broadcaster,
                    fu,
                    {"symbol": fu.symbol},
                    tracker_state,
                    now=orphan_now,
                    send_telegram=send_telegram,
                ):
                    orphan_sent.add(fu.message_key)
            if orphan_sent:
                _record_followup_side_effects(
                    orphan_events,
                    sent_keys=orphan_sent,
                    now=orphan_now,
                    pump_store=pump_store,
                )
        if ticker_by_sym and tracker_state.get("signals"):
            price_map = {
                sym: float(t.get("last_price") or 0)
                for sym, t in ticker_by_sym.items()
            }
            resolved = auto_resolve_active_signals(
                tracker_state,
                price_map,
                now=clock.now_utc(),
            )
            if resolved:
                LOG.info("watch_auto_resolve", closed=resolved)
        if send_telegram and broadcaster is not None:
            await get_advisory_digest().maybe_flush(broadcaster)
        from hunt_core.runtime.tick_state import hunt_scan_store

        hunt_scan_store().put_many(rows)
        _tick_success = True
        return rows
    finally:
        if _tick_success:
            _save_state(state)
            buffer_tracker_state(tracker_state)
            flush_lake()


def _error_row(symbol: str, error: str, now: Any) -> dict[str, Any]:
    """Minimal reject row so the universe-health assessor still sees a per-symbol failure."""
    return {
        "ts": now.isoformat(),
        "symbol": symbol,
        "error": error,
        "tick_path": "rest_error",
        "plane": "hunt",
    }


def _settle_native_results(
    ordered: list[str],
    results: list[Any],
    *,
    now: Any,
) -> dict[str, tuple[dict[str, Any] | None, NativeAnalystView | None]]:
    """Fold ``gather(return_exceptions=True)`` output into per-symbol ``(err_row|None, nav|None)``.

    A successful item is the ``(sym, err_row, nav)`` triple ``_native_one`` returned; an unhandled
    exception (raised outside its caught set — e.g. a Polars ``ComputeError``) becomes an error row so
    every OTHER symbol's already-assembled view survives instead of the whole gather raising.
    ``CancelledError`` re-propagates — shutdown must never be swallowed into a row.
    """
    settled: dict[str, tuple[dict[str, Any] | None, NativeAnalystView | None]] = {}
    for sym, res in zip(ordered, results, strict=True):
        if isinstance(res, asyncio.CancelledError):
            raise res
        if isinstance(res, BaseException):
            LOG.warning("snapshot_unhandled_exc", symbol=sym, error=repr(res))
            settled[sym] = (_error_row(sym, repr(res), now), None)
        else:
            _sym, err_row, nav = res
            settled[sym] = (err_row, nav)
    return settled


__all__ = ["run_tick"]
