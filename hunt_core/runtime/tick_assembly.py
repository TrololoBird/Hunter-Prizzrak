"""Full tick assembly orchestration (P2 — snapshot + scoring + lifecycle)."""
from __future__ import annotations

import asyncio
import structlog
from datetime import UTC, datetime
from typing import Any

from hunt_core.data.collect import (
    SnapshotTier,
    _book_from_pack,
    _fetch_rest_pack,
    _overlay_ws_market,
    kline_limits,
    resolve_kline_map,
    safe_fetch,
)
from hunt_core.data.completeness import (
    REQUIRED_SIGNAL_KLINE_TFS,
    audit_kline_integrity,
    audit_market_derivatives,
    repair_kline_map_gaps,
    stamp_market_freshness,
)
from hunt_core.contract import stamp_market_derivatives_provenance
from hunt_core.errors import finite_float_or_none
from hunt_core.features.prepare import _prepare_frame, prepare_symbol
from hunt_core.features.prepare_columns import (
    book_walls_from_depth,
    patch_work_4h,
    resolve_prepare_groups_for_symbol,
    should_bypass_kline_integrity,
    should_use_young_lite_path,
    violations_are_partial_history_only,
)
from hunt_core.features import snapshot as _snapshot_mod
from hunt_core.features.snapshot import (
    WatchMode,
    apply_cross_exchange_flat,
    apply_rest_enrichments_local,
    attach_cross_market_fields,
    attach_pp_flags,
    btc_beta_1h,
    btc_corr_1h,
    data_quality_report,
    enrich_work_research_frames,
    impulse_context,
    kline_integrity_reject,
    lite_prepared,
    market_snapshot,
    merge_ws_kline_closed,
    regime_snapshot,
    session_stats,
    squeeze_watch,
    stamp_derivative_zscores,
    tf_snapshot,
    tf_snapshot_for_symbol,
    tf_snapshot_lite,
)
from hunt_core.scanner.detect.delivery_support import liquidity_skip_reason
from hunt_core.features.structure import assess_market_structure
from hunt_core.data.tick_jsonl import ensure_fusion_lifecycle_fields
from hunt_core.features.factors import build_factor_panel
from hunt_core.runtime.state import current_symbol_state
from hunt_core.data.universe import PINNED_SYMBOLS

_CONFIRM_STICKY_MAX_TICKS = 6


def _stamp_setup_risk_reward(setup: dict[str, Any], *, direction: str) -> None:
    """Worst-edge R:R on the tick row so gates/cards never see risk_reward=None (G3)."""
    from hunt_core.contract import compute_setup_risk_reward

    ez = setup.get("entry_zone") or []
    if len(ez) < 2 or setup.get("stop_loss") is None or setup.get("tp1") is None:
        return
    rr = compute_setup_risk_reward(setup, direction=direction)
    if rr is not None:
        setup["risk_reward"] = round(float(rr), 4)


def _apply_dump_confirm_sticky(
    symbol: str,
    *,
    confirmed: bool,
    confirm_hard: list[str],
    lifecycle: Any,
    dump: dict[str, Any] | None = None,
) -> tuple[bool, list[str]]:
    """Hold dump confirm across 1–2 transient demote ticks (orderflow / bar gap)."""
    store = current_symbol_state()
    sym = symbol.upper()
    setup = dump if isinstance(dump, dict) else {}
    if setup.get("levels_viable") is False or setup.get("levels_veto"):
        store.confirm_sticky.pop(sym, None)
        if not confirmed:
            return confirmed, confirm_hard
    # (removed: `invalidate_short` lifecycle flag has no producer anywhere — the branch
    # was dead, G-69.)
    if confirmed:
        if setup.get("levels_viable") is False or setup.get("levels_veto"):
            return False, ["veto_levels:" + ",".join(setup.get("levels_veto") or ["not_viable"])]
        store.confirm_sticky[sym] = {
            "impulse_confirmed": True,
            "hard": list(confirm_hard),
            "ticks": 0,
        }
        return confirmed, confirm_hard
    latched = store.confirm_sticky.get(sym)
    if not isinstance(latched, dict) or not latched.get("impulse_confirmed"):
        return confirmed, confirm_hard
    ticks = int(latched.get("ticks") or 0) + 1
    latched["ticks"] = ticks
    store.confirm_sticky[sym] = latched
    if ticks <= _CONFIRM_STICKY_MAX_TICKS:
        return True, list(latched.get("hard") or confirm_hard)
    store.confirm_sticky.pop(sym, None)
    return confirmed, confirm_hard


from hunt_core.data_readiness import assess_symbol_data_readiness
from hunt_core.regime.market_regime import symbol_regime_features
from hunt_core.domain.schemas import SymbolFrames, UniverseSymbol
from hunt_core.features.fib import leg_fib_levels
from hunt_core.market import (
    HuntCcxtClient,
    HuntCcxtSpotCompanion,
    HuntCcxtStreams,
    normalize_depth_levels,
    resolve_live_price,
)
from hunt_core.runtime.state import SymbolStateStore, merge_hunt_extremes

LOG = structlog.get_logger("hunt_core.runtime.tick_assembly")


def _patch_market_live(
    market: dict[str, Any],
    *,
    prepared: Any,
    pack: dict[str, Any],
    book: dict[str, Any],
    ws_snap: dict[str, Any] | None,
    price: float,
) -> None:
    """Lightweight market refresh for hot carry — no full market_snapshot."""
    if price > 0:
        market["last_price"] = price
    if getattr(prepared, "oi_current", None) is not None:
        market["oi"] = prepared.oi_current
    if getattr(prepared, "oi_change_pct", None) is not None:
        market["oi_change_pct"] = prepared.oi_change_pct
    if getattr(prepared, "taker_ratio", None) is not None:
        market["taker_5m"] = prepared.taker_ratio
    if book.get("bid_price"):
        market["bid_price"] = book["bid_price"]
        market["ask_price"] = book.get("ask_price")
    if isinstance(pack.get("oi"), (int, float)):
        market["oi"] = pack["oi"]
    if getattr(prepared, "basis_pct", None) is not None:
        market["basis_pct"] = prepared.basis_pct
        if market.get("basis_bps") is None:
            market["basis_bps"] = round(float(prepared.basis_pct) * 100.0, 2)
    if getattr(prepared, "premium_zscore_5m", None) is not None:
        market["premium_zscore_5m"] = prepared.premium_zscore_5m
    if getattr(prepared, "premium_slope_5m", None) is not None:
        market["premium_slope_5m"] = prepared.premium_slope_5m
    if getattr(prepared, "mark_index_spread_bps", None) is not None:
        market["mark_index_spread_bps"] = prepared.mark_index_spread_bps
    if ws_snap:
        # ws_snap exposes the live book imbalance as "live_depth_imbalance", not
        # "depth_imbalance" — the old key here was a phantom, so hot-carry ticks
        # kept serving the stale carried book (audit G). Gate on ws_connected
        # like _overlay_ws_market does for `prepared`.
        live_di = ws_snap.get("live_depth_imbalance")
        if live_di is not None and ws_snap.get("ws_connected"):
            market["depth_imbalance"] = float(live_di)
            market["depth_imbalance_source"] = "ws_book"
        live_mp = ws_snap.get("live_microprice_bias")
        if live_mp is not None and ws_snap.get("ws_connected"):
            market["microprice_bias"] = float(live_mp)
            market["microprice_bias_source"] = "ws_book"
        for key in (
            "liquidation_score_5m",
            "liquidation_long_notional_5m",
            "liquidation_short_notional_5m",
            "agg_trade_delta_60s",
            "agg_trade_delta_30s",
            "ws_cvd_1m",
            "ws_cvd_5m",
            "ws_price_chg_1m",
            "ws_price_chg_5m",
            "basis_bps_live",
            "mark_live",
            "live_mark_price",
            "live_funding_rate",
        ):
            val = ws_snap.get(key)
            if val is not None:
                if key.startswith("liquidation_score"):
                    from hunt_core.contract import parse_liquidation_score

                    val = parse_liquidation_score(val)
                    if val is None:
                        continue
                market[key] = val


def _refresh_tf_stale_flags(tf: dict[str, Any]) -> None:
    for _stf in REQUIRED_SIGNAL_KLINE_TFS:
        closed_key = f"{_stf}_closed" if _stf != "1m" else "1m_closed"
        block = tf.get(closed_key) or tf.get(_stf) or {}
        close_ms = block.get("close_time_ms") if isinstance(block, dict) else None
        if close_ms is None:
            tf[f"stale_{_stf}"] = True
        else:
            from hunt_core.data.completeness import TF_MS

            interval = TF_MS.get(_stf, 300_000)
            age = int(datetime.now(UTC).timestamp() * 1000) - int(close_ms)
            tf[f"stale_{_stf}"] = age > int(interval * 2.5)


def _ensure_kinematic_row_fields(
    result: dict[str, Any],
    ticker: dict[str, Any] | None,
) -> None:
    """Backfill chg fields so kinematic gate is not blocked on hot/carry ticks."""
    if result.get("chg_24h_pct") is None and isinstance(ticker, dict):
        raw = ticker.get("price_change_percent")
        if raw is not None:
            try:
                result["chg_24h_pct"] = round(float(raw), 2)
            except (TypeError, ValueError):
                LOG.warning("kinematic_chg24_parse_failed raw=%s", raw)
    tf = result.get("timeframes")
    if not isinstance(tf, dict):
        return
    for key in ("1h", "1h_closed"):
        block = tf.get(key)
        if not isinstance(block, dict):
            continue
        if block.get("change_pct") is not None or block.get("price_change_pct") is not None:
            continue
        try:
            _candle = block.get("candle")
            candle = _candle if isinstance(_candle, dict) else {}
            o = float(candle.get("open") or block.get("open") or 0)
            c = float(candle.get("close") or block.get("close") or 0)
            if o > 0:
                block["change_pct"] = round((c - o) / o * 100.0, 2)
        except (TypeError, ValueError):
            continue


async def snapshot_symbol(
    client: HuntCcxtClient,
    settings: Any,
    minimums: dict[str, int],
    symbol: str,
    *,
    watch_mode: WatchMode,
    prev_oi: float | None,
    premium_all: dict[str, dict[str, float]],
    funding_info_all: dict[str, dict[str, float | int]],
    btc_work_1h: Any | None,
    exchange_by_sym: dict[str, Any],
    ticker_by_sym: dict[str, dict[str, Any]],
    ws_feed: HuntCcxtStreams | None = None,
    spot_companion: HuntCcxtSpotCompanion | None = None,
    stagger_klines_ms: int = 0,
    tier: SnapshotTier = "full",
    symbol_state: SymbolStateStore | None = None,
    kline_limits_override: dict[str, int] | None = None,
    kline_map_override: dict[str, Any] | None = None,
    enrichment_pack_override: dict[str, Any] | None = None,
    btc_work_1m: Any | None = None,
    hunt_fusion: bool = True,
    intra_bar: Any | None = None,
    allow_low_liquidity: bool = False,
) -> dict[str, Any]:
    meta = exchange_by_sym.get(symbol)
    ticker = ticker_by_sym.get(symbol)
    if meta is None or ticker is None:
        return {
            "ts": datetime.now(UTC).isoformat(),
            "symbol": symbol,
            "error": f"symbol_meta_or_ticker_missing:{symbol}",
        }
    last_price = finite_float_or_none(ticker.get("last_price"))
    quote_volume = finite_float_or_none(ticker.get("quote_volume"))
    missing_fields = [
        name
        for name, val in (("last_price", last_price), ("quote_volume", quote_volume))
        if val is None
    ]
    if missing_fields:
        return {
            "ts": datetime.now(UTC).isoformat(),
            "symbol": symbol,
            "error": f"data.ticker_field_missing:{','.join(missing_fields)}",
        }
    assert last_price is not None
    price = last_price
    market_row = {
        "symbol": symbol,
        "base_asset": meta.base_asset,
        "quote_asset": meta.quote_asset,
        "contract_type": meta.contract_type,
        "status": meta.status,
        "onboard_date_ms": meta.onboard_date_ms,
        "quote_volume": quote_volume,
        "price_change_percent": float(ticker.get("price_change_percent") or 0),
        "price_change_pct": float(ticker.get("price_change_percent") or 0),
        "last_price": price,
        "trade_count": float(ticker.get("trade_count") or 0),
    }
    item = UniverseSymbol(
        symbol=symbol,
        base_asset=meta.base_asset,
        quote_asset=meta.quote_asset,
        contract_type=meta.contract_type,
        status=meta.status,
        onboard_date_ms=meta.onboard_date_ms,
        quote_volume=market_row["quote_volume"],
        price_change_pct=market_row["price_change_percent"],
        last_price=price,
        shortlist_bucket="dump_watch",
        seed_source="hunt_scan",
        strategy_fits=(),
    )
    limits = kline_limits_override if kline_limits_override is not None else kline_limits(minimums, symbol)
    hot_tier = tier == "hot"
    kline_map: dict[str, Any] = {}
    fetch_errors: dict[str, str] = {}
    if kline_map_override is not None:
        kline_map = dict(kline_map_override)
    elif stagger_klines_ms > 0 and tier == "full":
        _base_tfs = ("1m", "5m", "15m", "1h", "4h", "1d")
        tf_order = _base_tfs + (("1w",) if "1w" in limits else ())
        for name in tf_order:
            res = await safe_fetch(
                lambda name=name: client.fetch_klines_cached(symbol, name, limit=limits[name]),
                context=f"klines.{symbol}.{name}",
                client=client,
            )
            kline_map[name] = res
            if res is None:
                fetch_errors[name] = "fetch_failed"
    else:
        snap_tier: SnapshotTier = "fast" if hot_tier else tier
        kline_map, fetch_errors = await resolve_kline_map(
            client, symbol, limits, tier=snap_tier, safe_fetch=safe_fetch
        )
    cache_delta_ready = False
    if hot_tier:
        from hunt_core.data.frame_cache import get_frame_cache

        cache_delta_ready = get_frame_cache().has_delta_ready(symbol)
    if not hot_tier or not cache_delta_ready:
        kline_map, fetch_errors = await repair_kline_map_gaps(
            client,
            symbol,
            kline_map,
            fetch_errors,
            required_tfs=REQUIRED_SIGNAL_KLINE_TFS,
        )

    def _bar_count(tf: str) -> int:
        raw = kline_map.get(tf)
        if raw is None or getattr(raw, "is_empty", lambda: True)():
            return 0
        return int(raw.height)

    if hot_tier or cache_delta_ready:
        from hunt_core.data.frame_cache import get_frame_cache

        cached = get_frame_cache().kline_map(symbol)
        if cached:
            restored_errors = dict(fetch_errors)
            for tf_key in REQUIRED_SIGNAL_KLINE_TFS:
                df = kline_map.get(tf_key)
                cdf = cached.get(tf_key)
                thin = df is None or getattr(df, "is_empty", lambda: True)() or (
                    tf_key == "1m" and df.height < 100
                )
                if thin and cdf is not None and not getattr(cdf, "is_empty", lambda: True)():
                    if cdf.height > (0 if df is None else int(df.height)):
                        kline_map[tf_key] = cdf
                        restored_errors.pop(tf_key, None)
            fetch_errors = restored_errors

    young_listing_bypass = should_bypass_kline_integrity(
        bars_4h=_bar_count("4h"),
        bars_1h=_bar_count("1h"),
        bars_15m=_bar_count("15m"),
    )

    integrity = audit_kline_integrity(
        kline_map,
        symbol=symbol,
        settings=settings,
        required_tfs=REQUIRED_SIGNAL_KLINE_TFS,
        fetch_errors=fetch_errors,
    )
    partial_history_ok = violations_are_partial_history_only(integrity.violations)
    if (
        not integrity.complete
        and not cache_delta_ready
        and not young_listing_bypass
        and not partial_history_ok
    ):
        return kline_integrity_reject(
            symbol=symbol,
            report=integrity,
            fetch_errors=fetch_errors,
        )
    df_1m = kline_map["1m"]
    df_5m = kline_map["5m"]
    if enrichment_pack_override is not None and enrichment_pack_override:
        pack = dict(enrichment_pack_override)
    else:
        pack = await _fetch_rest_pack(
            client, symbol, tier="fast" if hot_tier else tier, ws_feed=ws_feed
        )
    # leverageBracket is signed USER_DATA — public-only hunt uses default liq tiers
    liq_skip = liquidity_skip_reason(
        quote_volume=market_row["quote_volume"],
        oi=float(pack.get("oi") or 0) if pack.get("oi") is not None else None,
        last_price=price,
        symbol=symbol,
    )
    if liq_skip and not allow_low_liquidity:
        return {
            "ts": datetime.now(UTC).isoformat(),
            "symbol": symbol,
            "error": liq_skip,
            "liquidity_skip": True,
        }
    if liq_skip:
        market_row["liquidity_warning"] = liq_skip
    book = _book_from_pack(pack)
    _book_depth = pack.get("book_depth")
    depth_raw = _book_depth if isinstance(_book_depth, dict) else {}
    book_bids = normalize_depth_levels(depth_raw.get("bids") or depth_raw.get("bid_levels"))
    book_asks = normalize_depth_levels(depth_raw.get("asks") or depth_raw.get("ask_levels"))
    frames = SymbolFrames(
        symbol=symbol,
        df_15m=kline_map["15m"],
        df_1h=kline_map["1h"],
        df_5m=df_5m,
        df_4h=kline_map["4h"],
        bid_price=book.get("bid_price"),
        ask_price=book.get("ask_price"),
        bid_qty=book.get("bid_qty"),
        ask_qty=book.get("ask_qty"),
        book_bids=book_bids or None,
        book_asks=book_asks or None,
        frame_source_flags=("frames_hot_delta",) if hot_tier else ("frames_rest_full",),
    )

    bars_4h = int(kline_map["4h"].height if kline_map.get("4h") is not None else 0)
    bars_1h = int(kline_map["1h"].height if kline_map.get("1h") is not None else 0)
    cached_prep = None
    hot_delta = False
    if hot_tier:
        from hunt_core.data.frame_cache import get_frame_cache

        cached_prep = get_frame_cache().get_prepared(symbol)
        hot_delta = cached_prep is not None

    carry_base: dict[str, Any] | None = None
    hot_carry = False
    if hot_tier and hot_delta:
        carry_base = get_frame_cache().get_carry_row(symbol)
        hot_carry = carry_base is not None

    if hot_delta and cached_prep is not None:
        prepared = cached_prep
        young_listing = young_listing_bypass or partial_history_ok
    else:
        prepared = prepare_symbol(item, frames, minimums=minimums, settings=settings)
        young_listing = young_listing_bypass or partial_history_ok
        if prepared is None:
            young_listing = True
            if should_use_young_lite_path(bars_4h=bars_4h, bars_1h=bars_1h):
                prepared = lite_prepared(kline_map, symbol=symbol)
            else:
                relaxed = {"5m": 144, "15m": 96, "1h": 24, "4h": 6}
                prepared = prepare_symbol(item, frames, minimums=relaxed, settings=settings)
                if prepared is None:
                    prepared = lite_prepared(kline_map, symbol=symbol)
                else:
                    patch_work_4h(prepared, kline_map, symbol=symbol)
        else:
            patch_work_4h(prepared, kline_map, symbol=symbol)

    prep_groups = resolve_prepare_groups_for_symbol(symbol)
    if hot_delta:
        from hunt_core.features.prepare import _cached_prepare_frame

        work_1m = _cached_prepare_frame(
            df_1m, symbol=symbol, interval="1m", active_groups=prep_groups
        )
        if df_5m is not None and not df_5m.is_empty():
            prepared.work_5m = _cached_prepare_frame(
                df_5m, symbol=symbol, interval="5m", active_groups=prep_groups
            )
    else:
        work_1m = _prepare_frame(df_1m, active_groups=prep_groups)
    prepared.work_1m = work_1m
    delta: float | None = None
    if prepared.work_15m is not None and not prepared.work_15m.is_empty():
        # delta is BEST-EFFORT optional (float | None; downstream handles None). Coins
        # without agg-trade data (e.g. thin new listings in the wider universe) simply
        # have no ``delta_ratio`` column → polars raises ColumnNotFoundError, which the
        # old (TypeError, ValueError, IndexError) tuple missed, so the exception escaped
        # and failed the WHOLE symbol snapshot (2026-07-11: ColumnNotFoundError traceback
        # in run_tick). Any read failure here must degrade to None, not drop the symbol.
        try:
            delta = float(prepared.work_15m.item(-1, "delta_ratio"))
        except Exception:
            delta = None
    premium_row = premium_all.get(symbol) or premium_all.get(symbol.upper())
    funding_info = funding_info_all.get(symbol) or funding_info_all.get(symbol.upper())
    apply_rest_enrichments_local(
        prepared,
        client=client,
        symbol=symbol,
        pack=pack,
        book=book,
        premium_row=premium_row,
        funding_info=funding_info,
        delta=delta,
    )
    if not young_listing and not hot_tier:
        readiness = assess_symbol_data_readiness(prepared, settings, universe_item=item, snapshot_tier=tier)
        if not readiness.ready:
            reason = readiness.reason or "data.not_ready"
            return {
                "ts": datetime.now(UTC).isoformat(),
                "symbol": symbol,
                "error": reason,
                "no_signal_reason": reason,
                "data_readiness": {
                    "ready": False,
                    "reason": reason,
                    "details": dict(readiness.details),
                },
            }

    if symbol != "BTCUSDT" and btc_work_1h is not None:
        corr = btc_corr_1h(prepared.work_1h, btc_work_1h)
        if corr is not None:
            prepared.btc_corr_1h = corr
        beta = btc_beta_1h(prepared.work_1h, btc_work_1h)
        if beta is not None:
            prepared.btc_beta_1h = beta

    if not hot_tier:
        enrich_work_research_frames(prepared)

    impulse = impulse_context(prepared.work_4h, prepared.work_1h, symbol)
    ih4, il4 = impulse["impulse_high_4h"], impulse["impulse_low_4h"]
    rest_h, rest_l = impulse["hunt_high"], impulse["hunt_low"]
    fib_4h = leg_fib_levels(ih4, il4, direction="down")
    session = session_stats(work_1m)

    if hot_carry and carry_base is not None:
        tf = dict(carry_base.get("timeframes") or {})
        tf["1m"] = tf_snapshot(work_1m)
        tf["1m_closed"] = tf_snapshot(work_1m, closed=True)
        if prepared.work_5m is not None and not prepared.work_5m.is_empty():
            tf["5m"] = tf_snapshot(prepared.work_5m)
            tf["5m_closed"] = tf_snapshot(
                prepared.work_5m, closed=True, candle_patterns=True
            )
        merge_ws_kline_closed(tf, symbol, ws_feed, tf_key="1m_closed")
        merge_ws_kline_closed(tf, symbol, ws_feed, tf_key="5m_closed")
        merge_ws_kline_closed(tf, symbol, ws_feed, tf_key="15m_closed")
        tf["stale_15m"] = _snapshot_mod._stale_15m_flag(tf)
        _refresh_tf_stale_flags(tf)
    else:
        if kline_map.get("1d") is not None:
            work_1d_snap = _prepare_frame(kline_map["1d"], active_groups=prep_groups)
            if work_1d_snap is not None and not work_1d_snap.is_empty():
                probe = tf_snapshot_for_symbol(work_1d_snap, symbol)
                tf_1d = (
                    probe
                    if probe.get("status") != "empty" and probe.get("rsi14") is not None
                    else tf_snapshot_lite(kline_map["1d"])
                )
            else:
                tf_1d = tf_snapshot_lite(kline_map["1d"])
        else:
            tf_1d = {"status": "empty"}

        tf = {
            "1m": tf_snapshot(work_1m),
            "1m_closed": tf_snapshot(work_1m, closed=True),
            "3m": {"status": "empty"},
            "3m_closed": {"status": "empty"},
            "5m": tf_snapshot(prepared.work_5m),
            "5m_closed": tf_snapshot(
                prepared.work_5m, closed=True, candle_patterns=True
            ),
            "15m": attach_pp_flags(
                tf_snapshot_for_symbol(prepared.work_15m, symbol), prepared.work_15m
            ),
            "15m_closed": attach_pp_flags(
                tf_snapshot_for_symbol(
                    prepared.work_15m, symbol, closed=True, candle_patterns=True
                ),
                prepared.work_15m,
                closed=True,
            ),
            "1h": attach_pp_flags(
                tf_snapshot_for_symbol(
                    prepared.work_1h,
                    symbol,
                    rsi_trendline=True,
                    hidden_stoch_div=True,
                    chart_patterns=True,
                ),
                prepared.work_1h,
            ),
            "1h_closed": attach_pp_flags(
                tf_snapshot_for_symbol(
                    prepared.work_1h,
                    symbol,
                    closed=True,
                    rsi_trendline=True,
                    hidden_stoch_div=True,
                    chart_patterns=True,
                ),
                prepared.work_1h,
                closed=True,
            ),
            "4h": tf_snapshot_for_symbol(
                prepared.work_4h, symbol, hidden_stoch_div=True, chart_patterns=True
            ),
            "4h_closed": tf_snapshot_for_symbol(
                prepared.work_4h,
                symbol,
                closed=True,
                hidden_stoch_div=True,
                chart_patterns=True,
            ),
            "1d": tf_1d,
        }
        from hunt_core.features.snapshot import enrich_tf_research_fields

        if prepared.work_4h is not None and not prepared.work_4h.is_empty():
            enrich_tf_research_fields(tf["4h"], prepared.work_4h)
            enrich_tf_research_fields(tf["4h_closed"], prepared.work_4h)
        if kline_map.get("1d") is not None:
            work_1d_m = _prepare_frame(kline_map["1d"], active_groups=prep_groups)
            if work_1d_m is not None and not work_1d_m.is_empty():
                enrich_tf_research_fields(tf["1d"], work_1d_m)
        if "1w" in limits and kline_map.get("1w") is not None:
            work_1w = _prepare_frame(kline_map["1w"], active_groups=prep_groups)
            tf["1w"] = (
                tf_snapshot_for_symbol(work_1w, symbol)
                if work_1w is not None and not work_1w.is_empty()
                else tf_snapshot_lite(kline_map["1w"])
            )
        merge_ws_kline_closed(tf, symbol, ws_feed, tf_key="1m_closed")
        merge_ws_kline_closed(tf, symbol, ws_feed, tf_key="5m_closed")
        merge_ws_kline_closed(tf, symbol, ws_feed, tf_key="15m_closed")
        tf["stale_15m"] = _snapshot_mod._stale_15m_flag(tf)
        _refresh_tf_stale_flags(tf)
        if prepared.work_15m is not None and not prepared.work_15m.is_empty():
            _regime_feats = symbol_regime_features(prepared.work_15m)
            for _tf_key in ("15m", "15m_closed"):
                _block = tf.get(_tf_key)
                if isinstance(_block, dict) and _block.get("status") != "empty":
                    if _regime_feats.get("return_entropy_50") is not None:
                        _block["return_entropy_50"] = _regime_feats["return_entropy_50"]
                    if _regime_feats.get("volume_regime_break"):
                        _block["volume_regime_break"] = True
    ws_snap = ws_feed.snapshot(symbol) if ws_feed is not None else None
    _overlay_ws_market(prepared, ws_snap)
    live_px, live_src = resolve_live_price(
        symbol,
        ws_feed=ws_feed,
        book=book,
        ws_snap=ws_snap,
        fallback=price,
    )
    if live_px > 0:
        price = live_px
        market_row["last_price"] = live_px

    carry_book_walls: Any = None
    carry_cross: Any = None
    carry_liq: Any = None
    carry_data_quality: Any = None
    if hot_carry and carry_base is not None:
        market = dict(carry_base.get("market") or {})
        regime = dict(carry_base.get("regime") or {})
        _patch_market_live(
            market,
            prepared=prepared,
            pack=pack,
            book=book,
            ws_snap=ws_snap,
            price=price,
        )
        if hot_tier and ws_feed is not None:
            await attach_cross_market_fields(
                market,
                client=client,
                symbol=symbol,
                ws_feed=ws_feed,
            )
        carry_book_walls = carry_base.get("book_walls")
        carry_cross = carry_base.get("cross_microstructure")
        carry_liq = carry_base.get("liquidity_scenarios")
        carry_data_quality = carry_base.get("data_quality")
    else:
        spot_extra = (
            spot_companion.enrichments_for(symbol)
            if spot_companion is not None
            else None
        )
        market = market_snapshot(
            prepared,
            pack=pack,
            book=book,
            premium_row=premium_row,
            ticker=ticker,
            ws_snap=ws_snap,
            spot_extra=spot_extra,
        )
        if hot_tier and ws_feed is not None:
            await attach_cross_market_fields(
                market,
                client=client,
                symbol=symbol,
                ws_feed=ws_feed,
            )
        elif not hot_tier:
            await attach_cross_market_fields(
                market,
                client=client,
                symbol=symbol,
                ws_feed=ws_feed,
            )
        regime = regime_snapshot(prepared)
        if prepared.work_15m is not None and not prepared.work_15m.is_empty():
            regime.update(symbol_regime_features(prepared.work_15m))
    if getattr(prepared, "btc_decoupled_pump", None):
        market["btc_decoupled_pump"] = True
    if getattr(prepared, "btc_decoupled_dump", None):
        market["btc_decoupled_dump"] = True
    pc = getattr(prepared, "pump_cycle", None)
    if isinstance(pc, dict):
        market["pump_cycle"] = pc
    stamp_derivative_zscores(
        market,
        pack=pack,
        client=client,
        symbol=symbol,
        prepared=prepared,
        ws_snap=ws_snap,
    )
    stamp_market_freshness(market, ws_snap, pack, client=client, symbol=symbol)
    stamp_market_derivatives_provenance(market)

    structure_ctx: dict[str, Any] = {**(market or {}), **(regime or {})}
    if hot_carry and isinstance(carry_cross, dict):
        structure_ctx["cross_microstructure"] = carry_cross
    structure = assess_market_structure(tf, price=price, market=structure_ctx)
    hunt_h, hunt_l = merge_hunt_extremes(
        symbol,
        price=price,
        rest_hunt_high=rest_h,
        rest_hunt_low=rest_l,
        lifecycle_phase="",
        market=market,
    )
    fib_hunt = leg_fib_levels(hunt_h, hunt_l, direction="down")
    fib = {**fib_4h, "hunt": fib_hunt}
    dq = (
        carry_data_quality
        if hot_carry and carry_data_quality is not None
        else data_quality_report(
            prepared,
            frames=frames,
            df_1m=df_1m,
            pack=pack,
            book=book,
            tf=tf,
        )
    )
    if isinstance(dq, dict):
        dq = dict(dq)
        dq["delivery_derivatives_missing"] = audit_market_derivatives(market, tier=tier)
    result: dict[str, Any] = {
        "ts": datetime.now(UTC).isoformat(),
        "snapshot_tier": tier,
        "tick_path": (
            "hot_carry"
            if hot_carry
            else (
                "hot_delta"
                if hot_delta
                else ("hot_ws" if hot_tier else "rest_snapshot")
            )
        ),
        "hot_tick_no_rest": hot_tier,
        "hot_delta": hot_delta,
        "hot_carry": hot_carry,
        "symbol": symbol,
        "watch_mode": watch_mode,
        "young_listing": young_listing,
        "price": price,
        "price_source": live_src if live_px > 0 else "ticker_batch",
        "chg_24h_pct": round(float(ticker.get("price_change_percent") or 0), 2),
        "vol_24h_m": market.get("vol_24h_m"),
        # NOTE: "positioning" was a byte-identical alias of "market" — it
        # doubled every JSONL row (~45% of file size). Readers fall back
        # market -> positioning for old rows.
        "market": market,
        "regime": regime,
        "timeframes": tf,
        "session": session,
        "squeeze": squeeze_watch(tf, market),
        "impulse": impulse,
        "impulse_high": hunt_h,
        "impulse_low": hunt_l,
        "fib": fib,
        "kline_limits": limits,
        "data_quality": dq,
        "book_walls": carry_book_walls
        if hot_carry and carry_book_walls is not None
        else book_walls_from_depth(pack.get("book_depth")),
        "cross_microstructure": carry_cross if hot_carry else None,
        "structure": structure,
        "_prepared": prepared,
    }
    if hot_carry and carry_liq is not None:
        result["liquidity_scenarios"] = carry_liq

    if symbol in PINNED_SYMBOLS and not hot_tier:
        try:
            import polars as pl
            from hunt_core.features.prepare_columns import align_series_to_klines

            mark_1d, index_1d = await asyncio.gather(
                safe_fetch(client.fetch_mark_ohlcv(symbol, "1d", limit=30)),
                safe_fetch(client.fetch_index_ohlcv(symbol, "1d", limit=30)),
            )
            if (
                mark_1d is not None
                and index_1d is not None
                and not mark_1d.is_empty()
                and not index_1d.is_empty()
            ):
                time_col = (
                    "time"
                    if "time" in mark_1d.columns
                    else ("open_time" if "open_time" in mark_1d.columns else None)
                )
                if time_col is not None:
                    mark_slim = mark_1d.select(
                        time_col,
                        pl.col("close").alias("mark_close"),
                    )
                    index_slim = index_1d.select(
                        time_col if time_col in index_1d.columns else "open_time",
                        pl.col("close").alias("index_close"),
                    )
                    aligned = align_series_to_klines(
                        mark_slim,
                        index_slim,
                        on=time_col,
                        left_cols=("mark_close",),
                        right_cols=("index_close",),
                    )
                else:
                    aligned = pl.DataFrame()
                if not aligned.is_empty():
                    latest_mark = float(aligned["mark_close"][-1])
                    latest_index = float(aligned["index_close"][-1])
                    if latest_index > 0:
                        result["mark_index_divergence_pct"] = round(
                            (latest_mark - latest_index) / latest_index * 100.0, 4
                        )
                    basis = (
                        (pl.col("mark_close") - pl.col("index_close"))
                        / pl.col("index_close")
                        * 100.0
                    )
                    basis_s = aligned.with_columns(basis.alias("basis_pct"))["basis_pct"]
                    if basis_s.len() >= 7:
                        result["mark_index_slope_7d"] = round(
                            float(basis_s[-1] - basis_s[-7]), 4
                        )
        except Exception as exc:
            LOG.warning(
                "mark_index_divergence_snapshot_failed | symbol=%s error=%s",
                symbol,
                exc,
            )
        try:
            from hunt_core.market import attach_cross_microstructure

            await attach_cross_microstructure(client, result)
            cx_walls = (result.get("cross_microstructure") or {}).get("book_walls")
            if isinstance(cx_walls, dict) and cx_walls.get("bid_levels"):
                result["book_walls"] = cx_walls
        except Exception as exc:
            LOG.warning("cross_microstructure_snapshot_failed | symbol=%s error=%s", symbol, exc)

    try:
        from hunt_core.maps.engine import apply_map_bundle_to_row, build_map_bundle, get_map_store

        cx = result.get("cross_microstructure") or {}
        vp_cross = cx.get("volume_profile_1h") if isinstance(cx, dict) else None
        liq_est = cx.get("liquidation_estimate") if isinstance(cx, dict) else None
        frame_map: dict[str, Any] = {}
        for tf_key, frame_key in (
            ("1h", "work_1h"),
            ("4h", "work_4h"),
            ("15m", "work_15m"),
            ("1d", "work_1d"),
            ("1w", "work_1w"),
        ):
            w = getattr(prepared, frame_key, None)
            if w is not None and hasattr(w, "is_empty") and not w.is_empty():
                frame_map[tf_key] = w
        store = get_map_store()
        oi_bars = store.get_cached_oi_bars(symbol)
        allow_oi_fetch = (not hot_tier) or (symbol in PINNED_SYMBOLS) or hot_carry
        if oi_bars is None and allow_oi_fetch and hasattr(client, "fetch_oi_bars_for_maps"):
            try:
                oi_bars = await client.fetch_oi_bars_for_maps(symbol, period="1h", limit=48)
                if oi_bars:
                    store.cache_oi_bars(symbol, oi_bars)
            except Exception as exc:
                LOG.warning("maps_oi_bars_fetch_failed | symbol=%s error=%s", symbol, exc)
                oi_bars = None
        if oi_bars is None and isinstance(liq_est, dict):
            _oi_data = liq_est.get("oi_bars")
            if isinstance(_oi_data, list):
                oi_bars = _oi_data
        deep_bids: list[tuple[float, float]] | None = None
        deep_asks: list[tuple[float, float]] | None = None
        book_walls = result.get("book_walls") if isinstance(result.get("book_walls"), dict) else None
        if isinstance(book_walls, dict):
            per_ex = book_walls.get("per_exchange") or {}
            primary = per_ex.get("binance") if isinstance(per_ex, dict) else None
            if isinstance(primary, dict):
                if isinstance(primary.get("bids"), list):
                    deep_bids = [(float(x[0]), float(x[1])) for x in primary["bids"] if len(x) >= 2]
                if isinstance(primary.get("asks"), list):
                    deep_asks = [(float(x[0]), float(x[1])) for x in primary["asks"] if len(x) >= 2]
        px_chg = None
        if isinstance(ws_snap, dict):
            px_chg = ws_snap.get("ws_price_chg_1m")
        bundle = build_map_bundle(
            symbol=symbol,
            current_price=price,
            ws_snap=ws_snap,
            book_walls=result.get("book_walls"),
            live_book=ws_feed.live_book(symbol) if ws_feed is not None else None,
            trades=ws_feed.trade_buffer(symbol) if ws_feed is not None else None,
            liq_buffers=ws_feed.liquidation_buffers() if ws_feed is not None else store.liq_buffers(symbol),
            frames=frame_map or None,
            cross_vp=vp_cross if isinstance(vp_cross, dict) else None,
            bracket_tiers=client.get_cached_leverage_tiers(symbol) if hasattr(client, "get_cached_leverage_tiers") else None,
            oi_bars=oi_bars,
            oi_usd=float(market.get("oi_usd") or 0) or None,
            # `global_ls_ratio` must be the GLOBAL account ratio; top-trader L/S is a
            # different population. Prefer the global series and fall back to top only
            # when it is missing (was the other way round, so forward-zone long_share
            # reflected top accounts while calling itself global).
            global_ls_ratio=float(market.get("global_ls_1h") or market.get("top_ls_1h") or 0) or None,
            # vol_24h_m is in MILLIONS of quote currency, hence the 1e6. `quote_volume_24h`
            # (raw USD by its name) used to sit first in this chain but is produced by
            # NOTHING — a phantom key that, the day someone added a producer, would have
            # been multiplied by 1e6 too, inflating daily_volume a millionfold and silently
            # zeroing every wall-significance score (sig = notional / daily_volume).
            daily_volume=float(market.get("vol_24h_m") or 0) * 1_000_000,
            price_change_pct=float(px_chg) if px_chg is not None else None,
            deep_bids=deep_bids,
            deep_asks=deep_asks,
            funding_rate=float(market.get("funding_rate") or market.get("live_funding_rate") or 0) or None,
            top_ls_ratio=float(market.get("top_ls_1h") or 0) or None,
            basis_pct=float(market.get("basis_pct") or 0) or None,
            oi_z=float(market.get("oi_z") or 0) or None,
            ws_cvd=float((ws_snap or {}).get("ws_cvd_5m") or 0) or None,
            store=store,
        )
        apply_map_bundle_to_row(result, bundle)
    except Exception as exc:
        LOG.warning("map_bundle_failed | symbol=%s error=%s", symbol, exc)

    apply_cross_exchange_flat(result)

    result["plane"] = "deep" if not hunt_fusion else "hunt"
    neutral_lc = ensure_fusion_lifecycle_fields({"phase": "neutral", "phase_fusion": "neutral"})
    result["lifecycle"] = neutral_lc
    stub = {
        "symbol": symbol,
        "impulse_confirmed": False,
        "phase": "neutral",
        "lifecycle_phase": "neutral",
        "lifecycle": neutral_lc,
        "young_listing": young_listing,
        "bars_1h": bars_1h,
    }
    result["dump"] = {**stub, "direction": "short"}
    result["long"] = {**stub, "direction": "long"}
    if hunt_fusion:
        merge_hunt_extremes(
            symbol,
            price=price,
            rest_hunt_high=rest_h,
            rest_hunt_low=rest_l,
            lifecycle_phase="neutral",
            market=market,
        )
    try:
        from hunt_core.confluence.mtf import build_mtf_confluence

        _timeframes = result.get("timeframes")
        _timeframes = _timeframes if isinstance(_timeframes, dict) else {}
        mtf_obj = build_mtf_confluence(
            symbol,
            _timeframes,
            float(result.get("price") or 0),
            market=market if isinstance(market, dict) else None,
            row=result,
        )
        if mtf_obj is not None:
            result["mtf"] = mtf_obj
            from hunt_core.data.tick_jsonl import mtf_to_json_dict

            summary = mtf_to_json_dict(mtf_obj)
            if isinstance(summary, dict):
                result["mtf_summary"] = {
                    "dominant": summary.get("dominant"),
                    "long_htf_count": summary.get("long_htf_count"),
                    "short_htf_count": summary.get("short_htf_count"),
                }
            # NB: row["htf_bias"] used to be written here, with a comment claiming the
            # "direction gate (analyst + scanner) can veto counter-trend signals" from it.
            # NOTHING ever read it: prizrak computes its own _htf_bias (vocabulary
            # long/short/neutral) and the scanner its own _htf_trend_bias (bull/bear).
            # Publishing a THIRD bias under a name the other two also use — in yet another
            # vocabulary — was a live trap: any `row["htf_bias"] == "bull"` a future reader
            # wrote would have silently no-op'd against a long/short value. Removed rather
            # than left as a decoy; the two engines that DO gate own their own bias.
    except Exception as exc:
        LOG.debug("mtf_confluence_skipped | symbol=%s error=%s", symbol, exc)
    if hunt_fusion:
        merge_hunt_extremes(
            symbol,
            price=price,
            rest_hunt_high=rest_h,
            rest_hunt_low=rest_l,
            lifecycle_phase=str((result.get("lifecycle") or {}).get("phase") or "neutral"),
            market=market,
        )

    result["factor_panel"] = build_factor_panel(result)

    try:
        from hunt_core.toolkit.forecast import stamp_forecasts_on_row

        if ws_feed is not None:
            from hunt_core.market import apply_live_price_to_row

            apply_live_price_to_row(result, ws_feed=ws_feed)
        stamp_forecasts_on_row(result)
        arch = result.get("entry_archetype")
        if arch:
            for setup_key in ("dump", "long"):
                setup_block = result.get(setup_key)
                if isinstance(setup_block, dict):
                    setup_block["entry_archetype"] = arch
    except Exception as exc:
        LOG.warning("fusion_forecast_stamp_failed | symbol=%s error=%s", symbol, exc)

    _ensure_kinematic_row_fields(result, ticker)

    if not result.get("error") and tier in ("full", "fast", "hot"):
        from hunt_core.data.frame_cache import get_frame_cache

        cache = get_frame_cache()
        cache.seed_klines(symbol, kline_map)
        cache.seed_enrichment(symbol, pack)
        if prepared is not None and (tier in ("full", "fast") or hot_delta):
            cache.seed_prepared(symbol, prepared)
        if tier in ("full", "fast") and not hot_carry:
            cache.seed_carry_row(symbol, result)
        elif (
            not hot_carry
            and tier == "hot"
            and str(result.get("tick_path") or "") in {"hot_ws", "hot_delta", "hot_bootstrap"}
        ):
            cache.seed_carry_row(symbol, result)

    return result


