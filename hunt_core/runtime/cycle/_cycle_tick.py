"""Per-tick watch loop — snapshot, delivery, follow-ups (Phase 8 split)."""
from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from typing import Any

from hunt_core import clock
from hunt_core.data.collect import (
    SnapshotTier,
    TickBatchCache,
    refresh_tick_batch_cache,
    safe_fetch,
    sort_symbols_for_tick,
)
from hunt_core.data.lake import (
    buffer_tracker_state,
    flush_lake,
)
from hunt_core.deliver.digest import get_advisory_digest
from hunt_core.deliver.telegram import TelegramBroadcaster
from hunt_core.errors import defensive_exc_types
from hunt_core.features.prepare import _prepare_frame
from hunt_core.features.feature_engine import FeatureExtractError, build_feature_vector
from hunt_core.market import (
    HuntCcxtClient,
    HuntCcxtSpotCompanion,
    HuntCcxtStreams,
    attach_cross_fields,
    merge_ws_cross_into_snapshot,
)
from hunt_core.runtime.cycle._cycle_reconcile import (
    _deliver_followup,
    _reconcile_inwatch_active,
    _reconcile_orphan_signals,
    _record_followup_side_effects,
)
from hunt_core.runtime.state import LOG, WatchMode, SymbolStateStore
from hunt_core.data.universe import PINNED_SYMBOLS, effective_watch_mode

from hunt_core.track.tracker import (
    auto_resolve_active_signals,
    evaluate_followups,
    iter_active_tracker_symbols,
    latch_row_setups,
    load_tracker_state,
    reconcile_active_from_ticker,
)
from hunt_core.runtime.tick_assembly import snapshot_symbol
from hunt_core.data.lake import FeatureLakeWriter


def _settle_snapshot_results(
    ordered: Sequence[str],
    results: Sequence[Any],
    *,
    now_iso: str,
    tier: str,
) -> list[tuple[str, dict[str, Any]]]:
    """Convert asyncio.gather(return_exceptions=True) output into (sym, row) pairs.

    A successful item is the (sym, dict) tuple _snapshot_one returned; an unhandled
    exception (one raised outside _snapshot_one's caught set) becomes an error row
    so the rest of the tick's rows survive instead of the whole gather raising.
    CancelledError re-propagates — shutdown must never be swallowed into a row.
    """
    pairs: list[tuple[str, dict[str, Any]]] = []
    for sym, res in zip(ordered, results, strict=True):
        if isinstance(res, asyncio.CancelledError):
            raise res
        if isinstance(res, BaseException):
            LOG.warning("snapshot_unhandled_exc", symbol=sym, error=repr(res))
            pairs.append((sym, {
                "ts": now_iso,
                "symbol": sym,
                "error": repr(res),
                "tick_path": "rest_error",
                "snapshot_tier": tier,
            }))
        else:
            pairs.append(res)
    return pairs


def _closed_bar_ts(prepared: Any) -> str | None:
    """Closed (penultimate) 15m bar close_time as a stable per-bar key.

    The feature lake must store one row per *closed* bar, not one per intra-bar
    tick — otherwise the robust-z distribution and magnitude history that the
    fusion gate compares against are built from ~30 forming-bar samples per bar.
    """
    work = getattr(prepared, "work_15m", None)
    if work is None:
        return None
    if getattr(work, "height", 0) < 2 or "close_time" not in getattr(work, "columns", []):
        return None
    try:
        return str(work.item(-2, "close_time"))
    except Exception:
        return None


async def run_tick(
    symbols: tuple[str, ...],
    *,
    settings: Any,
    minimums: dict[str, int],
    client: HuntCcxtClient,
    prev_oi: dict[str, float | None],
    last_bias: dict[str, str],
    last_lifecycle_phase: dict[str, str],
    mode_map: dict[str, WatchMode],
    broadcaster: TelegramBroadcaster | None,
    send_telegram: bool,
    ticker_by_sym: dict[str, dict[str, Any]] | None = None,
    ignition_by_sym: dict[str, dict[str, Any]] | None = None,
    pump_stats_by_sym: dict[str, dict[str, Any]] | None = None,
    pump_store: Any | None = None,
    ws_feed: HuntCcxtStreams | None = None,
    spot_companion: HuntCcxtSpotCompanion | None = None,
    batch_cache: TickBatchCache | None = None,
    tier: SnapshotTier = "full",
    cross_ex_cache: dict[str, dict[str, Any]] | None = None,
    prescan_outlier_by_sym: dict[str, dict[str, Any]] | None = None,
    symbol_state: SymbolStateStore | None = None,
    feature_lake: FeatureLakeWriter | None = None,
    tier_by_symbol: dict[str, SnapshotTier] | None = None,
    snapshot_parallel: int | None = None,
    intra_bar: Any | None = None,
) -> list[dict[str, Any]]:
    from hunt_core.runtime.cycle import _impl as _tick_impl

    _load_state = _tick_impl._load_state
    _save_state = _tick_impl._save_state
    _phase_long = _tick_impl._phase_long
    _overlay_ws_tickers = _tick_impl._overlay_ws_tickers
    _refresh_live_price = _tick_impl._refresh_live_price
    HUNT_SNAPSHOT_PARALLEL = _tick_impl.HUNT_SNAPSHOT_PARALLEL
    SYMBOL_TICK_TIMEOUT_S = _tick_impl.SYMBOL_TICK_TIMEOUT_S
    state = _load_state()
    tracker_state = load_tracker_state()
    now = clock.now_utc()
    rows: list[dict[str, Any]] = []

    def _tier_for(sym: str) -> SnapshotTier:
        if tier_by_symbol and sym in tier_by_symbol:
            return tier_by_symbol[sym]
        return tier

    batch_tier: SnapshotTier = (
        "full" if any(_tier_for(s) == "full" for s in symbols) else tier
    )
    parallel = max(1, int(snapshot_parallel or HUNT_SNAPSHOT_PARALLEL))
    _tick_success = False
    try:
        cache = batch_cache or TickBatchCache()
        need_btc = any(s != "BTCUSDT" for s in symbols)
        await asyncio.wait_for(
            refresh_tick_batch_cache(
                cache,
                client,
                safe_fetch=safe_fetch,
                prepare_frame=_prepare_frame,
                need_btc=need_btc,
                tier=batch_tier,
            ),
            timeout=120.0,
        )
        premium_all = cache.premium_all
        funding_info_all = cache.funding_info_all
        exchange_by_sym = cache.exchange_by_sym
        btc_work_1h = cache.btc_work_1h
        btc_work_1m = cache.btc_work_1m
        if ticker_by_sym is None:
            ticker_raw = await asyncio.wait_for(
                safe_fetch(
                    client.fetch_ticker_24h,
                    context="ticker_24h",
                    client=client,
                ),
                timeout=120.0,
            ) or []
            ticker_by_sym = {str(t.get("symbol")): t for t in ticker_raw if t.get("symbol")}
        if batch_tier == "full" and spot_companion is not None and symbols:
            full_syms = [s for s in symbols if _tier_for(s) == "full"]
            futures_mids = {
                s: float((ticker_by_sym.get(s) or {}).get("last_price") or 0) or None
                for s in full_syms
            }
            try:
                spot_n = await spot_companion.refresh_symbols(
                    full_syms, futures_mid_by_symbol=futures_mids
                )
                LOG.debug("spot_companion_refresh", symbols=len(full_syms), updated=spot_n)
            except defensive_exc_types(asyncio.IncompleteReadError, OSError, ConnectionError) as exc:
                LOG.warning("spot_companion_refresh_failed", error=repr(exc))

        ordered = sort_symbols_for_tick(
            symbols,
            ignition_by_sym=ignition_by_sym,
            last_bias=last_bias,
        )
        if tier == "fast":
            LOG.debug("watch_tick_fast_tier", symbols=len(ordered), head=list(ordered[:4]))

        _overlay_ws_tickers(ticker_by_sym, ordered, ws_feed)
        tick_started = time.monotonic()

        async def _snapshot_one(sym: str) -> tuple[str, dict[str, Any]]:
            sym_tier = _tier_for(sym)
            mode = effective_watch_mode(
                sym,
                mode_map,
                lifecycle_bias=last_bias.get(sym),
            )
            try:
                row = await asyncio.wait_for(
                    snapshot_symbol(
                        client,
                        settings,
                        minimums,
                        sym,
                        watch_mode=mode,
                        prev_oi=prev_oi.get(sym),
                        premium_all=premium_all,
                        funding_info_all=funding_info_all,
                        btc_work_1h=btc_work_1h,
                        btc_work_1m=btc_work_1m,
                        exchange_by_sym=exchange_by_sym,
                        ticker_by_sym=ticker_by_sym,
                        ws_feed=ws_feed,
                        spot_companion=spot_companion,
                        pump_stats=(
                            pump_stats_by_sym.get(sym) if pump_stats_by_sym else None
                        ),
                        tier=sym_tier,
                        symbol_state=symbol_state,
                        intra_bar=intra_bar,
                    ),
                    timeout=SYMBOL_TICK_TIMEOUT_S,
                )
                return sym, row
            except TimeoutError:
                LOG.warning(
                    "watch_symbol_timeout",
                    symbol=sym,
                    timeout_s=SYMBOL_TICK_TIMEOUT_S,
                )
                return sym, {
                    "ts": now.isoformat(),
                    "symbol": sym,
                    "error": "symbol_tick_timeout",
                    "tick_path": "rest_error",
                    "snapshot_tier": tier,
                }
            except defensive_exc_types(asyncio.IncompleteReadError) as exc:
                LOG.warning("dump_symbol_failed", symbol=sym, error=repr(exc))
                return sym, {
                    "ts": now.isoformat(),
                    "symbol": sym,
                    "error": repr(exc),
                    "tick_path": "rest_error",
                    "snapshot_tier": tier,
                }

        sem = asyncio.Semaphore(parallel)

        async def _bounded_snapshot(sym: str) -> tuple[str, dict[str, Any]]:
            async with sem:
                return await _snapshot_one(sym)

        # return_exceptions=True so ONE symbol raising an exception outside
        # _snapshot_one's caught set (e.g. a Polars ComputeError) does not sink the
        # whole tick — every other symbol's already-computed row would otherwise be
        # discarded. Unhandled failures become error rows (like the in-loop handler);
        # cancellation still propagates.
        raw_results = await asyncio.gather(
            *[_bounded_snapshot(s) for s in ordered], return_exceptions=True
        )
        snap_pairs = _settle_snapshot_results(ordered, raw_results, now_iso=now.isoformat(), tier=tier)
        row_by_sym = dict(snap_pairs)
        snap_elapsed = round(time.monotonic() - tick_started, 2)
        if len(ordered) > 1:
            full_n = sum(1 for s in ordered if _tier_for(s) == "full")
            LOG.info(
                "watch_snapshot_batch",
                symbols=len(ordered),
                parallel=parallel,
                elapsed_s=snap_elapsed,
                tier=tier,
                full_symbols=full_n,
                fast_symbols=len(ordered) - full_n,
                used_weight_1m=client.used_weight_1m(),  # Binance IP budget; cap 2400/min
            )

        for symbol in ordered:
            try:
                row = row_by_sym.get(symbol)
                if row is None:
                    continue
                if row.get("error"):
                    LOG.info(
                        "watch_symbol_data_reject",
                        symbol=symbol,
                        error=row.get("error"),
                        no_signal_reason=row.get("no_signal_reason"),
                        violations=(row.get("data_violations") or [])[:4],
                    )
                    rows.append(row)
                    continue
                if (
                    feature_lake is not None
                    and _tier_for(symbol) == "full"
                    and symbol not in PINNED_SYMBOLS
                ):
                    prepared_obj = row.get("_prepared")
                    closed_bar_ts = _closed_bar_ts(prepared_obj)
                    lake_key = f"{symbol}:lake_bar"
                    # One lake row per CLOSED 15m bar (not per intra-bar tick), built
                    # from closed-bar values only — keeps the fusion reference
                    # distribution causal and un-oversampled (Phase 0.1).
                    if closed_bar_ts is not None and state.get(lake_key) != closed_bar_ts:
                        try:
                            vector = build_feature_vector(
                                prepared_obj,
                                row,
                                symbol=symbol,
                                tf="15m",
                                require_closed=True,
                            )
                            feature_lake.enqueue(
                                symbol, str(row.get("ts")), "15m", vector.to_dict()
                            )
                            state[lake_key] = closed_bar_ts
                        except FeatureExtractError as exc:
                            LOG.warning(
                                "feature_lake_enqueue_skipped",
                                symbol=symbol,
                                error=str(exc),
                            )
                row = latch_row_setups(tracker_state, row)
                oi_val = (row.get("market") or row.get("positioning") or {}).get("oi")
                if oi_val is not None:
                    prev_oi[symbol] = float(oi_val)
                if cross_ex_cache and symbol in cross_ex_cache:
                    cx = dict(cross_ex_cache[symbol])
                    if ws_feed is not None:
                        cx = merge_ws_cross_into_snapshot(
                            cx,
                            ws_feed.live_funding_cross(symbol),
                        )
                    attach_cross_fields(row, cx)
                if not row.get("error"):
                    row["plane"] = "hunt"
                    from hunt_core.data.tick_jsonl import (
                        ensure_fusion_lifecycle_fields,
                        resolve_row_mtf,
                    )

                    active = row.get("long") if (row.get("long") or {}).get("impulse_confirmed") else row.get("dump")
                    row["lifecycle"] = ensure_fusion_lifecycle_fields(
                        row.get("lifecycle") if isinstance(row.get("lifecycle"), dict) else None,
                        setup=active if isinstance(active, dict) else None,
                    )
                    mtf = resolve_row_mtf(row, symbol=symbol)
                    if mtf is not None:
                        row["mtf"] = mtf
                rows.append(row)
                if ignition_by_sym and symbol in ignition_by_sym:
                    row["ignited"] = True
                    row["ignition"] = ignition_by_sym[symbol]
                if prescan_outlier_by_sym and symbol in prescan_outlier_by_sym:
                    row["prescan_outlier"] = prescan_outlier_by_sym[symbol]
                if pump_stats_by_sym and symbol in pump_stats_by_sym:
                    row["pump_history"] = pump_stats_by_sym[symbol]
                kline_events = await _reconcile_inwatch_active(
                    client, tracker_state, symbol=symbol, now=now
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
                        row,
                        tracker_state,
                        now=now,
                        send_telegram=send_telegram,
                    ):
                        followup_sent_keys.add(fu.message_key)
                followups = evaluate_followups(tracker_state, row, now=now)
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
                        row,
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

        # Ticker safety net: symbols rotated out of this tick's batch still get
        # SL/TP extremes from the already-fetched 24h ticker (MEGA @ SL while
        # last_checked froze — universe rotation gap).
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
                ws_feed=ws_feed,
            )
            if ticker_events:
                LOG.info(
                    "watch_ticker_reconcile",
                    symbols=sorted(missing_active),
                    events=len(ticker_events),
                )

        # Orphan reconciliation: active signals whose symbol left the watchlist
        # would otherwise never close (PLAYUSDT held TP2 for 18h unnoticed).
        orphan_events = await _reconcile_orphan_signals(
            client, tracker_state, seen_symbols=seen, now=clock.now_utc()
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



__all__ = ["run_tick"]
