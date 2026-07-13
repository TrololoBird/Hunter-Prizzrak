"""On-demand symbol analysis for /signal — rate-limited, separate REST client."""
from __future__ import annotations



import asyncio
import html
import logging
import re
from typing import Any

LOG = logging.getLogger("hunt_core.runtime.symbol_probe")

# klines.<tf>.stale.<SYMBOL>.<age>ms><limit>ms  (completeness.audit_kline_staleness)
_STALE_RE = re.compile(r"^klines\.([0-9a-z]+)\.stale\.[A-Z0-9]+\.(\d+)ms>(\d+)ms$")
# klines.<tf>.<reason>  — fetch_failed / empty_frame / staleness.*
_KLINE_FETCH_RE = re.compile(r"^klines\.([0-9a-z]+)\.(fetch_failed|empty_frame|staleness\.[a-z_]+)$")


def humanize_probe_error(err: str, *, symbol: str) -> str | None:
    """Turn a raw data-integrity violation into a plain, actionable TG message.

    Returns None for unrecognized codes so the caller can fall back to the raw
    string. Presentation only — does not change what the signal gate rejects.
    The 4h/1h HTF frames are REST-only, so right after a restart (or a rate-limit
    pause) they lag until backfill catches up; the user hit exactly that window.
    """
    short = symbol.replace("USDT", "")
    m = _STALE_RE.match(err.strip())
    if m:
        tf, age_ms, limit_ms = m.group(1), int(m.group(2)), int(m.group(3))
        age_h = age_ms / 3_600_000
        limit_h = limit_ms / 3_600_000
        return (
            f"📉 Свечи <b>{tf}</b> устарели: данные ~{age_h:.1f}ч назад "
            f"(порог {limit_h:.0f}ч) — HTF-контекст временно недоступен.\n"
            f"Обычно самовосстанавливается за несколько минут после рестарта "
            f"бота, пока 1h/4h догружаются по REST. Повтори через пару минут "
            f"или запроси свежие данные: <code>/signal {short} --live</code>"
        )
    m = _KLINE_FETCH_RE.match(err.strip())
    if m:
        tf = m.group(1)
        return (
            f"🌐 Не удалось загрузить свечи <b>{tf}</b> (REST) — данные неполные.\n"
            f"Повтори позже или запроси свежие: <code>/signal {short} --live</code>"
        )
    return None

from hunt_core.data.collect import (
    SnapshotTier,
    TickBatchCache,
    probe_kline_limits,
    refresh_tick_batch_cache,
    safe_fetch,
)
from hunt_core.runtime.tick_assembly import snapshot_symbol
from hunt_core.domain.config import load_settings
from hunt_core.features.prepare import _prepare_frame, min_required_bars
from hunt_core.market import HuntCcxtClient
from hunt_core.deliver.telegram import TelegramBroadcaster

from hunt_core.data.tick_jsonl import btc_market_context, resolve_trade_direction
from hunt_core.track.events import append_audit_log, audit_probe_row, backtest_levels_on_bars
from hunt_core.track.tracker import load_tracker_state
from hunt_core.data.universe import PINNED_SYMBOLS
from hunt_core.params.store import effective_hunt_params
from hunt_core.data.universe import add_to_watchlist, register_signal_notify

_STAGGER_MS = 150
_PROBE_TIMEOUT_S = 240.0
_PINNED_PROBE_TIMEOUT_S = 360.0
_FAST_PROBE_TIMEOUT_S = 45.0


def normalize_symbol(raw: str) -> str:
    sym = raw.strip().upper().replace("/", "").replace("-", "")
    if not sym:
        return ""
    if sym.endswith("USDC"):
        return sym
    return sym if sym.endswith("USDT") else f"{sym}USDT"


def parse_symbol_text(text: str) -> str:
    """Plain chat text → symbol (btc, BEAT, ETHUSDT) without /command."""
    raw = text.strip().upper()
    if not raw or raw.startswith("/"):
        return ""
    raw = raw.replace("/", "").replace("-", "")
    if " " in raw:
        parts = [p for p in raw.split() if p]
        if len(parts) == 1:
            raw = parts[0]
        elif parts[0] in {"SIGNAL", "SIG", "СИГНАЛ"} and len(parts) >= 2:
            raw = parts[1]
        else:
            return ""
    return normalize_symbol(raw)


def _is_hunt_anomaly(row: dict[str, Any], *, symbol: str) -> bool:
    cal = effective_hunt_params(symbol)
    sess = row.get("session") or {}
    chg = abs(float(row.get("chg_24h_pct") or 0))
    rng = float(sess.get("range_pct_24h") or 0)
    if bool(row.get("young_listing")):
        return True
    return chg >= cal.anomaly_min_chg_24h_pct or rng >= cal.anomaly_min_range_24h_pct


async def probe_symbol_signal(
    symbol: str,
    *,
    stagger_ms: int = _STAGGER_MS,
    auto_watchlist: bool = True,
    probe_kind: str = "signal",
    client: HuntCcxtClient | None = None,
    tier: SnapshotTier | None = None,
    batch_cache: TickBatchCache | None = None,
    ticker_by_sym: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Full hunt analysis for one symbol using an isolated REST client.

    ``probe_kind="catalog"`` — shadow scan for /signals: no watchlist, no tracker
    backtest, lighter enrichments. ``probe_kind="signal"`` — /signal point query.
    ``probe_kind="delivery"`` — fast dev probe (tier=fast, shared batch cache).
    """
    sym = normalize_symbol(symbol)
    if not sym:
        return {"symbol": symbol, "error": "empty_symbol"}

    catalog_probe = probe_kind == "catalog"
    delivery_probe = probe_kind == "delivery"
    if probe_kind == "signal" and not delivery_probe:
        from hunt_core.runtime.query_service import STORE_STALE_S, row_age_seconds
        from hunt_core.runtime.tick_state import last_tick_store

        cached = last_tick_store().resolve(sym)
        if isinstance(cached, dict) and not cached.get("error"):
            age = row_age_seconds(cached)
            if age is not None and age <= STORE_STALE_S:
                out = dict(cached)
                out["_query_source"] = "tick_store"
                return out

    if catalog_probe:
        auto_watchlist = False

    snap_tier: SnapshotTier = tier or ("fast" if delivery_probe else "full")
    lite_probe = delivery_probe or snap_tier == "fast"
    if lite_probe and stagger_ms > 0 and delivery_probe:
        stagger_ms = 0

    settings = load_settings()
    minimums = min_required_bars(
        min_bars_15m=settings.filters.min_bars_15m,
        min_bars_1h=settings.filters.min_bars_1h,
        min_bars_4h=settings.filters.min_bars_4h,
    )
    owned_plane = None
    if client is None:
        from hunt_core.market.factory import create_hunt_market_plane_from_settings

        owned_plane = await create_hunt_market_plane_from_settings(settings)
        client = owned_plane.client
    if not getattr(client, "_markets_loaded", False):
        await client.load_markets()
    from hunt_core.market.symbol_gate import is_allowed_for_analysis

    if not is_allowed_for_analysis(sym, exchange=client.exchange):
        return {
            "symbol": sym,
            "error": "symbol_not_tradable",
            "detail": "delisted or not in Binance USD-M CCXT markets",
        }
    probe_timeout = (
        _FAST_PROBE_TIMEOUT_S
        if lite_probe
        else (_PINNED_PROBE_TIMEOUT_S if sym in PINNED_SYMBOLS else _PROBE_TIMEOUT_S)
    )
    cache = batch_cache
    if cache is None:
        cache = TickBatchCache()
    try:
        await refresh_tick_batch_cache(
            cache,
            client,
            safe_fetch=safe_fetch,
            prepare_frame=_prepare_frame,
            need_btc=sym != "BTCUSDT",
            tier=snap_tier,
        )
        premium_all = cache.premium_all
        funding_info_all = cache.funding_info_all
        exchange_by_sym = cache.exchange_by_sym
        btc_work_1h = cache.btc_work_1h
        btc_work_4h = cache.btc_work_4h
        btc_work_1m = cache.btc_work_1m

        if ticker_by_sym is None:
            ticker_raw = await safe_fetch(client.fetch_ticker_24h(), context="ticker_24h") or []
            ticker_by_sym = {str(t.get("symbol")): t for t in ticker_raw if t.get("symbol")}

        kline_override = probe_kline_limits(minimums, sym) if delivery_probe else None
        row = await asyncio.wait_for(
            snapshot_symbol(
                client,
                settings,
                minimums,
                sym,
                watch_mode="both",
                prev_oi=None,
                premium_all=premium_all,
                funding_info_all=funding_info_all,
                btc_work_1h=btc_work_1h,
                btc_work_1m=btc_work_1m,
                exchange_by_sym=exchange_by_sym,
                ticker_by_sym=ticker_by_sym,
                ws_feed=None,
                spot_companion=None,
                stagger_klines_ms=0 if lite_probe else stagger_ms,
                tier=snap_tier,
                kline_limits_override=kline_override,
            ),
            timeout=probe_timeout,
        )
        if btc_work_1h is not None:
            row["btc_context"] = btc_market_context(btc_work_1h, btc_work_4h=btc_work_4h)
        # Ручной /signal (и прочие explicit-пробы) дают полный направленный разбор
        # по ЛЮБОМУ символу, включая якоря BTC/ETH/XAU/XAG вне аномалии. Meme-only
        # фильтр остаётся только в пассивном сканере (watch.py). Якорь при низкой
        # волатильности помечается явным флагом, но НЕ блокируется.
        if sym in PINNED_SYMBOLS:
            row["_pinned_reference"] = True
            row["_low_volatility_anchor"] = not _is_hunt_anomaly(row, symbol=sym)
        if not catalog_probe:
            # MTF confluence (pinned + any explicit /signal symbol with frames)
            try:
                from hunt_core.confluence.mtf import build_mtf_confluence

                tf = row.get("timeframes") or {}
                price = float(row.get("price") or 0)
                if tf and price > 0:
                    row["mtf"] = build_mtf_confluence(
                        sym, tf, price, market=row.get("market"), row=row
                    )
            except Exception as _mtf_exc:
                LOG.warning("mtf_confluence_failed | sym=%s error=%s", sym, _mtf_exc)
            if not lite_probe:
                # Cross-exchange (Binance-listed symbol vs Bybit/OKX/Bitget)
                try:
                    from hunt_core.market.cross import attach_cross_fields

                    cx = await asyncio.wait_for(
                        client.fetch_cross_exchange_snapshot(sym),
                        timeout=30.0,
                    )
                    if isinstance(cx, dict):
                        attach_cross_fields(row, cx)
                except Exception as _cx_exc:
                    LOG.warning("cross_exchange_failed | sym=%s error=%s", sym, _cx_exc)
                if sym in PINNED_SYMBOLS:
                    try:
                        from hunt_core.market.cross import attach_cross_microstructure

                        await attach_cross_microstructure(client, row)
                        cx_micro = row.get("cross_microstructure") or {}
                        cross_walls = cx_micro.get("book_walls")
                        if isinstance(cross_walls, dict) and cross_walls.get("bid_levels"):
                            row["book_walls"] = cross_walls
                    except Exception as _cm_exc:
                        LOG.warning("cross_microstructure_failed | sym=%s error=%s", sym, _cm_exc)
                    try:
                        from hunt_core.maps.engine import apply_map_bundle_to_row, build_map_bundle, get_map_store

                        store = get_map_store()
                        oi_bars = store.get_cached_oi_bars(sym)
                        cx = row.get("cross_microstructure") or {}
                        liq_est = cx.get("liquidation_estimate") if isinstance(cx, dict) else None
                        if oi_bars is None and isinstance(liq_est, dict):
                            oi_bars = liq_est.get("oi_bars")
                        prepared = row.get("_prepared")
                        frame_map: dict[str, Any] = {}
                        if prepared is not None:
                            for tf_key, frame_key in (
                                ("1h", "work_1h"),
                                ("4h", "work_4h"),
                                ("15m", "work_15m"),
                                ("1d", "work_1d"),
                            ):
                                w = getattr(prepared, frame_key, None)
                                if w is not None and hasattr(w, "is_empty") and not w.is_empty():
                                    frame_map[tf_key] = w
                        book_walls = row.get("book_walls") if isinstance(row.get("book_walls"), dict) else None
                        deep_bids: list[tuple[float, float]] | None = None
                        deep_asks: list[tuple[float, float]] | None = None
                        if isinstance(book_walls, dict):
                            per_ex = book_walls.get("per_exchange") or {}
                            primary = per_ex.get("binance") if isinstance(per_ex, dict) else None
                            if isinstance(primary, dict):
                                if isinstance(primary.get("bids"), list):
                                    deep_bids = [
                                        (float(x[0]), float(x[1])) for x in primary["bids"] if len(x) >= 2
                                    ]
                                if isinstance(primary.get("asks"), list):
                                    deep_asks = [
                                        (float(x[0]), float(x[1])) for x in primary["asks"] if len(x) >= 2
                                    ]
                        market = row.get("market") or {}
                        bundle = build_map_bundle(
                            symbol=sym,
                            current_price=float(row.get("price") or 0),
                            book_walls=book_walls,
                            frames=frame_map or None,
                            cross_vp=(cx.get("volume_profile_1h") if isinstance(cx, dict) else None),
                            oi_bars=oi_bars if isinstance(oi_bars, list) else None,
                            oi_usd=float(market.get("oi_usd") or 0) or None,
                            global_ls_ratio=float(market.get("top_ls_1h") or market.get("global_ls_1h") or 0)
                            or None,
                            deep_bids=deep_bids,
                            deep_asks=deep_asks,
                            store=store,
                        )
                        apply_map_bundle_to_row(row, bundle)
                    except Exception as _map_exc:
                        LOG.debug("symbol_probe_maps_failed | sym=%s error=%s", sym, _map_exc)
        audit_source = (
            "delivery_probe"
            if delivery_probe
            else ("signals_cmd" if catalog_probe else "signal_cmd")
        )
        audit = audit_probe_row(row, source=audit_source)
        if not catalog_probe and not lite_probe:
            bt = await _tracker_levels_backtest(client, sym)
            if bt:
                audit["tracker_backtest"] = bt
        if not delivery_probe:
            append_audit_log(audit)
        row["_signal_audit"] = audit
        if delivery_probe:
            row["_probe_kind"] = "delivery"
        elif catalog_probe:
            row["_probe_kind"] = "catalog"
        # PrizrakTrade (hunt_core/prizrak/) is the sole decision authority; the report
        # reads row["prizrak_summary"] / row["prizrak_signals"].

        if auto_watchlist and not row.get("error"):
            dump = row.get("dump") or {}
            long_setup = row.get("long") or {}
            lc = row.get("lifecycle") or {}
            bias = str(lc.get("recommended_bias") or "both")
            watch_bias = bias if bias in {"short", "long", "both"} else "both"
            fuel = max(
                float(dump.get("dump_fuel") or 0),
                float(long_setup.get("long_fuel") or 0),
            )
            added = add_to_watchlist(
                sym,
                source="signal_cmd",
                hunt_score=fuel,
                watch_bias=watch_bias,
                note=f"signal_probe phase={dump.get('phase')}",
            )
            direction, _, _, _ = resolve_trade_direction(row)
            notify_phase = (
                "dump_imminent" if direction == "short" else "long_imminent"
            )
            register_signal_notify(
                sym,
                direction=direction,
                phase=notify_phase,
                notify_on_forming=True,
            )
            row["_watchlist_added"] = added
        return row
    finally:
        if owned_plane is not None:
            await owned_plane.aclose()


async def probe_symbol_catalog(
    symbol: str,
    *,
    stagger_ms: int = 120,
) -> dict[str, Any]:
    """Shadow catalog snapshot for /signals — no watchlist or tracker side effects."""
    return await probe_symbol_signal(
        symbol,
        stagger_ms=stagger_ms,
        probe_kind="catalog",
        auto_watchlist=False,
    )


async def probe_pinned_deep(
    symbol: str,
    *,
    stagger_ms: int = 200,
    auto_watchlist: bool = True,
    client: HuntCcxtClient | None = None,
) -> dict[str, Any]:
    """Extended REST + full prepare + microstructure for pinned anchors."""
    from hunt_core.data.universe import cache_is_fresh, load_pinned_cache
    from hunt_core.runtime.analyst_assembly import assemble_analyst_tick
    from hunt_core.runtime.query_service import STORE_STALE_S, row_age_seconds
    from hunt_core.runtime.tick_state import deep_query_store

    sym = normalize_symbol(symbol)
    cached = deep_query_store().resolve(sym)
    if isinstance(cached, dict) and not cached.get("error") and cached.get("prizrak_summary"):
        age = row_age_seconds(cached)
        if age is not None and age <= STORE_STALE_S:
            out = dict(cached)
            out["_query_source"] = "deep_store"
            if cache_is_fresh(sym):
                out["_pinned_cache"] = load_pinned_cache(sym)
            return out
    if client is None:
        msg = f"probe_deep: no client for {sym}"
        raise RuntimeError(msg)
    row = await assemble_analyst_tick(sym, client, stagger_ms=max(stagger_ms, 200))
    if cache_is_fresh(sym):
        row["_pinned_cache"] = load_pinned_cache(sym)
    return row


async def probe_deep_only(
    symbol: str,
    *,
    stagger_ms: int = 200,
    client: HuntCcxtClient | None = None,
) -> dict[str, Any]:
    """Deep product probe — enrich pinned/MTF without watchlist side effects."""
    sym = normalize_symbol(symbol)
    if sym in PINNED_SYMBOLS:
        row = await probe_pinned_deep(
            sym, stagger_ms=max(stagger_ms, 200), auto_watchlist=False, client=client
        )
    else:
        row = await probe_symbol_signal(
            sym,
            stagger_ms=stagger_ms,
            auto_watchlist=False,
            client=client,
        )
        row["_analyst"] = True
    return row


async def _tracker_levels_backtest(client: Any, sym: str) -> dict[str, Any] | None:
    """Mini forward backtest: replay latched levels of the active tracker signal
    over closed 5m bars since open; lets /signal audit compare outcome vs tracker."""
    from datetime import UTC, datetime

    state = load_tracker_state()
    for key, sig in (state.get("signals") or {}).items():
        if not key.startswith(f"{sym}:") or sig.get("status") != "active":
            continue
        direction = str(sig.get("direction") or "")
        try:
            opened = datetime.fromisoformat(str(sig.get("opened_at")))
        except (ValueError, TypeError):
            return None
        age_min = (datetime.now(UTC) - opened).total_seconds() / 60.0
        limit = min(1000, max(12, int(age_min / 5) + 2))
        try:
            df = await client.fetch_klines_cached(sym, "5m", limit=limit)
        except Exception as exc:
            LOG.warning("tracker_backtest_klines_failed | sym=%s error=%s", sym, exc)
            return None
        if df is None or df.is_empty():
            return None
        df = df.filter(df["open_time"] >= opened)
        if df.is_empty():
            return None
        bars = list(zip(df["high"].to_list(), df["low"].to_list(), df["close"].to_list(), strict=True))
        setup = {
            "entry_zone": [sig.get("entry_lo"), sig.get("entry_hi")],
            "stop_loss": sig.get("stop_loss"),
            "tp1": sig.get("tp1"),
            "tp2": sig.get("tp2"),
        }
        result = backtest_levels_on_bars(bars, setup=setup, direction=direction)
        result["signal_key"] = key
        result["opened_at"] = str(sig.get("opened_at"))
        result["tracker_tp1_hit"] = bool(sig.get("tp1_hit"))
        return result
    return None



async def deliver_signal_probe(
    broadcaster: TelegramBroadcaster,
    symbol: str,
    *,
    stagger_ms: int = _STAGGER_MS,
    live: bool = False,
    client: HuntCcxtClient | None = None,
    allow_low_liquidity: bool = False,
) -> dict[str, Any]:
    """Reply with query result for ``symbol`` (store-first, full blocker explain).

    Telegram ``/signal`` calls this directly via ``telegram_commands.py``.
    """
    sym = normalize_symbol(symbol)

    from hunt_core.runtime.query_service import (
        build_query_result,
        format_freshness_footer,
        format_query_telegram,
        resolve_query_row,
    )

    row, _source, from_store, age_s = await resolve_query_row(
        sym, live=live, stagger_ms=stagger_ms, client=client, allow_low_liquidity=allow_low_liquidity
    )
    audit = row.get("_signal_audit") or {}
    liq_warn = row.get("liquidity_warning")
    if row.get("error"):
        extra = ""
        if audit.get("issues"):
            extra = "\n<i>audit: " + html.escape(", ".join(audit["issues"][:3])) + "</i>"
        raw_err = str(row["error"])
        friendly = humanize_probe_error(raw_err, symbol=sym)
        if friendly is not None:
            # Lead with the plain explanation; keep the raw code small for power users/logs.
            body = f"{friendly}\n<i>({html.escape(raw_err)})</i>"
        else:
            body = f"<code>{html.escape(raw_err)}</code>"
        await broadcaster.send_html(
            f"⚠️ <b>/signal</b> {html.escape(sym)}\n{body}{extra}",
            no_split=True,
        )
        return row

    query = build_query_result(
        row, sym, source=_source, from_store=from_store, age_s=age_s
    )
    text = format_query_telegram(
        query, added_watch=bool(row.get("_watchlist_added"))
    )
    if liq_warn:
        text = f"⚠️ <b>Низкая ликвидность:</b> <code>{html.escape(liq_warn)}</code>\n\n{text}"
    if row.get("_probe_lite"):
        text = (
            f"{text}\n⚡ <i>облегчённый пак: серии basis/OI/long-short не запрошены "
            f"(защита от IP-бана, символ вне юниверса)</i>"
        )
    from hunt_core.deliver.confluence_grid import build_confluence_grid, format_grid_telegram
    from hunt_core.deliver._sections import format_intraday_maps_telegram

    # Skip level grid + maps for WAIT signals — avoid conflicting scanner artifacts
    _prizrak_action = str((row.get("prizrak_summary") or {}).get("action") or "").upper()
    _show_extras = _prizrak_action in {"LONG", "SHORT"} or not _prizrak_action
    if _show_extras:
        grid = build_confluence_grid(row)
        if grid:
            text = f"{text}\n\n{format_grid_telegram(grid, price=float(row.get('price') or 0))}"
        maps_block = format_intraday_maps_telegram(row)
        if maps_block:
            text = f"{text}\n\n{maps_block}"
    text = f"{text}\n{format_freshness_footer(query)}"
    # Deep analysis for a low-cap can exceed one Telegram message (many levels);
    # split into tag-safe parts (📄 1/N) instead of truncating the tail away.
    await broadcaster.send_html(text)
    return row
