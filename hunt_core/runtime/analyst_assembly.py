"""Module 1 Deep tick orchestrator — pinned continuous + on-demand query plane."""
from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import structlog

from hunt_core.data.universe import PINNED_SYMBOLS, save_pinned_cache
from hunt_core.market import HuntCcxtClient
from hunt_core.paths import ANALYST_TICKS_JSONL
from hunt_core.data.tick_jsonl import serialize_tick_row

LOG = structlog.get_logger("hunt.analyst_assembly")

_STALE_HOURS_DEFAULT = 4.0


def analyst_pinned_interval_s() -> float:
    return float(os.getenv("HUNT_DEEP_PINNED_INTERVAL", "300") or 300)


def deep_tg_on_change() -> bool:
    return os.getenv("HUNT_DEEP_TG_ON_CHANGE", "1").strip().lower() not in {
        "0",
        "false",
        "no",
    }


def deep_tg_stale_hours() -> float:
    return float(os.getenv("HUNT_DEEP_TG_STALE_HOURS", str(_STALE_HOURS_DEFAULT)) or _STALE_HOURS_DEFAULT)


def append_deep_tick_jsonl(row: dict[str, Any]) -> None:
    from hunt_core.data.jsonl_io import append_jsonl_lines
    from hunt_core.diagnostics.tick_diagnostics import append_tick_diagnostics

    append_tick_diagnostics(row)
    ANALYST_TICKS_JSONL.parent.mkdir(parents=True, exist_ok=True)
    append_jsonl_lines(ANALYST_TICKS_JSONL, [serialize_tick_row(row)])


def material_deep_change(
    symbol: str,
    row: dict[str, Any],
    *,
    prev: dict[str, Any] | None,
    now: datetime | None = None,
) -> bool:
    """True when verdict action changed — telemetry only (TG uses lifecycle spine)."""
    _ = symbol
    if prev is None:
        return True
    prev_summary = prev.get("prizrak_summary") if isinstance(prev.get("prizrak_summary"), dict) else {}
    summary = row.get("prizrak_summary") if isinstance(row.get("prizrak_summary"), dict) else {}
    if str(prev_summary.get("action") or "wait") != str(summary.get("action") or "wait"):
        return True
    if str(prev_summary.get("path") or "") != str(summary.get("path") or ""):
        return True
    now = now or datetime.now(UTC)
    ts = row.get("ts") or prev.get("ts")
    try:
        dt = datetime.fromisoformat(str(ts))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        age_h = (now - dt).total_seconds() / 3600.0
        return age_h >= deep_tg_stale_hours()
    except (TypeError, ValueError):
        LOG.warning("material_deep_change_ts_parse_failed", symbol=symbol, ts=ts)
        return False


async def assemble_analyst_tick(
    symbol: str,
    client: HuntCcxtClient,
    *,
    stagger_ms: int = 200,
    ws_feed: Any | None = None,
) -> dict[str, Any]:
    """Full deep snapshot — no hunt fusion, structure-first enrichments."""
    import asyncio

    from hunt_core.prizrak.build import _enrich_analyst_row
    from hunt_core.domain.config import load_settings
    from hunt_core.features.prepare import _prepare_frame
    from hunt_core.features.prepare import min_required_bars
    from hunt_core.runtime.tick_assembly import snapshot_symbol
    from hunt_core.data.collect import safe_fetch

    sym = str(symbol or "").upper()
    settings = load_settings()
    minimums = min_required_bars(
        min_bars_15m=settings.filters.min_bars_15m,
        min_bars_1h=settings.filters.min_bars_1h,
        min_bars_4h=settings.filters.min_bars_4h,
    )
    owned_plane = None
    if client is None:
        from hunt_core.market import create_hunt_market_plane_from_settings

        owned_plane = await create_hunt_market_plane_from_settings(settings)
        client = owned_plane.client
    if not getattr(client, "_markets_loaded", False):
        await client.load_markets()

    premium_all = await safe_fetch(client.fetch_premium_index_all(), context="premium_index_all") or {}
    await asyncio.sleep(stagger_ms / 1000.0)
    funding_info_all = await safe_fetch(client.fetch_funding_info_all(), context="funding_info_all") or {}
    await asyncio.sleep(stagger_ms / 1000.0)
    exchange_list = await safe_fetch(client.fetch_exchange_symbols(), context="exchange_symbols") or []
    exchange_by_sym = {r.symbol: r for r in exchange_list}
    await asyncio.sleep(stagger_ms / 1000.0)
    ticker_raw = await safe_fetch(client.fetch_ticker_24h(), context="ticker_24h") or []
    ticker_by_sym = {str(t.get("symbol")): t for t in ticker_raw if t.get("symbol")}

    btc_work_1h = None
    btc_work_4h = None
    btc_work_1m = None
    btc_df = await safe_fetch(client.fetch_klines_cached("BTCUSDT", "1h", limit=500), context="btc_klines_1h")
    if btc_df is not None and not btc_df.is_empty():
        btc_work_1h = _prepare_frame(btc_df)
    btc_4h = await safe_fetch(client.fetch_klines_cached("BTCUSDT", "4h", limit=250), context="btc_klines_4h")
    if btc_4h is not None and not btc_4h.is_empty():
        btc_work_4h = _prepare_frame(btc_4h)
    btc_1m = await safe_fetch(client.fetch_klines_cached("BTCUSDT", "1m", limit=999), context="btc_klines_1m")
    if btc_1m is not None and not btc_1m.is_empty():
        btc_work_1m = _prepare_frame(btc_1m)

    old_full = os.environ.get("HUNT_FULL_PREPARE")
    os.environ["HUNT_FULL_PREPARE"] = "1"
    try:
        row = await snapshot_symbol(
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
            ws_feed=ws_feed,
            spot_companion=None,
            stagger_klines_ms=stagger_ms,
            tier="full",
            hunt_fusion=False,
        )
    finally:
        if old_full is None:
            os.environ.pop("HUNT_FULL_PREPARE", None)
        else:
            os.environ["HUNT_FULL_PREPARE"] = old_full

    if row.get("error"):
        if owned_plane is not None:
            await owned_plane.close()
        return row

    if btc_work_1h is not None:
        from hunt_core.data.tick_jsonl import btc_market_context

        row["btc_context"] = btc_market_context(btc_work_1h, btc_work_4h=btc_work_4h)

    try:
        from hunt_core.features.microstructure import build_microstructure_context

        market = dict(row.get("market") or {})
        market["symbol"] = sym
        ms_by_dir: dict[str, Any] = {}
        for direction in ("long", "short"):
            try:
                ms_by_dir[direction] = build_microstructure_context({**market, "direction": direction})
            except Exception as exc:
                LOG.warning("deep_microstructure_failed", symbol=sym, direction=direction, error=repr(exc))
        if ms_by_dir:
            row["microstructure_by_direction"] = ms_by_dir
    except Exception as exc:
        LOG.warning("deep_microstructure_pack_failed", symbol=sym, error=repr(exc))

    prizrak_ohlcv_by_tf: dict[str, list[list[float]]] = {}
    try:
        from hunt_core.prizrak.config import PrizrakConfig

        pcfg = PrizrakConfig.load()
        for tier in (pcfg.intraday, pcfg.meso, pcfg.macro):
            for tf_name in tier.timeframes:
                if tf_name in prizrak_ohlcv_by_tf:
                    continue
                try:
                    # Fetch ≥130 bars: candidate/structure logic slices raw[-lookback:]
                    # (tier.lookback_bars=60 for meso) so this does NOT change their
                    # behaviour, but interest-zone detection needs ~120 bars to see BOTH
                    # the support-below and resistance-above structural boxes.
                    fetch_limit = max(int(tier.lookback_bars), 130)
                    bars = await safe_fetch(
                        client.fetch_ohlcv_list_cached(sym, tf_name, limit=fetch_limit),
                        context=f"prizrak_ohlcv_{tf_name}",
                    )
                except Exception as exc:
                    LOG.warning("prizrak_ohlcv_fetch_failed", symbol=sym, tf=tf_name, error=repr(exc))
                    bars = None
                if bars:
                    prizrak_ohlcv_by_tf[tf_name] = bars
                await asyncio.sleep(stagger_ms / 1000.0)
    except Exception:
        LOG.exception("prizrak_ohlcv_prep_failed", symbol=sym)

    row = _enrich_analyst_row(row, ohlcv_by_tf=prizrak_ohlcv_by_tf or None)
    LOG.info(
        "prizrak_enrich_done",
        symbol=sym,
        tfs_fetched=sorted(prizrak_ohlcv_by_tf.keys()),
        summary_action=(row.get("prizrak_summary") or {}).get("action"),
        candidates=len(row.get("prizrak_signals") or []),
    )
    try:
        from hunt_core.toolkit.manipulation_fusion import stamp_fusion_on_row

        stamp_fusion_on_row(row)
    except Exception as exc:
        LOG.debug("deep_manipulation_fusion_skipped", symbol=sym, error=repr(exc))

    # PrizrakTrade (hunt_core/prizrak/, wired in _enrich_analyst_row above) is the
    # sole decision authority.
    row["plane"] = "deep"
    row["_analyst"] = True
    row["tick_path"] = "analyst_assembly"

    from datetime import UTC, datetime

    now = datetime.now(UTC)
    row["as_of"] = now.isoformat()
    dom_ts = None
    cx = row.get("cross_microstructure") if isinstance(row.get("cross_microstructure"), dict) else {}
    walls = cx.get("book_walls") if isinstance(cx.get("book_walls"), dict) else row.get("book_walls")
    if isinstance(walls, dict) and walls.get("fetched_at"):
        dom_ts = walls.get("fetched_at")
    row_ts = row.get("ts")
    try:
        tick_dt = datetime.fromisoformat(str(row_ts).replace("Z", "+00:00")) if row_ts else now
        if tick_dt.tzinfo is None:
            tick_dt = tick_dt.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        LOG.warning("analyst_tick_dt_parse_failed", symbol=sym, row_ts=row_ts)
        tick_dt = now
    dom_age_s: float | None = None
    if dom_ts:
        try:
            dom_dt = datetime.fromisoformat(str(dom_ts).replace("Z", "+00:00"))
            if dom_dt.tzinfo is None:
                dom_dt = dom_dt.replace(tzinfo=UTC)
            dom_age_s = (now - dom_dt).total_seconds()
        except (TypeError, ValueError):
            LOG.warning("analyst_dom_ts_parse_failed", symbol=sym, dom_ts=dom_ts)
            dom_age_s = (now - tick_dt).total_seconds()
    else:
        dom_age_s = (now - tick_dt).total_seconds()
    row["freshness"] = {
        "as_of": row["as_of"],
        "tick_age_s": round((now - tick_dt).total_seconds(), 1),
        "dom_age_s": round(dom_age_s, 1) if dom_age_s is not None else None,
    }

    from hunt_core.toolkit.forecast import stamp_forecasts_on_row

    stamp_forecasts_on_row(row)

    try:
        save_pinned_cache(sym, row)
    except Exception as exc:
        LOG.warning("analyst_pinned_cache_failed", symbol=sym, error=repr(exc))

    from hunt_core.runtime.tick_state import deep_query_store

    deep_query_store().put(sym, row)
    append_deep_tick_jsonl(row)
    try:
        from hunt_core.prizrak.engines.calibration import (
            CALIBRATION_JSON,
            merge_live_sample,
            write_calibration_rollup,
        )

        summary = row.get("prizrak_summary")
        if isinstance(summary, dict):
            if CALIBRATION_JSON.is_file():
                import json as _json

                report = _json.loads(CALIBRATION_JSON.read_text(encoding="utf-8"))
                report = merge_live_sample(report, summary, sym)
                CALIBRATION_JSON.write_text(
                    _json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
                )
            else:
                write_calibration_rollup(limit=200)
    except Exception as exc:
        LOG.debug("prizrak_calibration_skip", symbol=sym, error=repr(exc))
    try:
        from hunt_core.prizrak.engines.config import load_analyst_config
        from hunt_core.prizrak.engines.signal_queue import refresh_pinned_signal_queue

        v2cfg = load_analyst_config()
        if getattr(v2cfg, "signal_queue_enabled", True):
            row["signal_queue"] = refresh_pinned_signal_queue(sym, row, top_n=v2cfg.signal_queue_top_n)
    except Exception as exc:
        LOG.debug("prizrak_signal_queue_skip", symbol=sym, error=repr(exc))
    if owned_plane is not None:
        await owned_plane.close()
    return row


async def send_analyst_change_telegram(
    broadcaster: Any,
    row: dict[str, Any],
    *,
    cycle_peers: list[dict[str, Any]] | None = None,
    lifecycle_event: str = "signal",
) -> bool:
    """Send deep analysis TG when material change detected."""
    import html

    from hunt_core.deliver.confluence_grid import build_confluence_grid, format_grid_telegram
    from hunt_core.deliver._sections import format_intraday_maps_telegram

    sym = str(row.get("symbol") or "").upper()
    if row.get("error"):
        return False
    summary = row.get("prizrak_summary") if isinstance(row.get("prizrak_summary"), dict) else {}
    action = str(summary.get("action") or "wait").lower()
    if action not in {"long", "short"}:
        LOG.info("analyst_pinned_tg_skipped_wait", symbol=sym, action=action)
        return False
    from hunt_core.prizrak.arbiter import evaluate_deep_delivery

    verdict = summary if summary else {}
    ok, blockers = evaluate_deep_delivery(symbol=sym, verdict=verdict)
    if not ok:
        LOG.info("analyst_pinned_tg_skipped_arbiter", symbol=sym, blockers=blockers)
        return False
    blocks: list[str] = []
    if lifecycle_event == "activated":
        sym_label = str(row.get("symbol") or "").upper().replace("USDT", "-USDT")
        summary = row.get("prizrak_summary") if isinstance(row.get("prizrak_summary"), dict) else {}
        rr = summary.get("rr_primary")
        rr_label = summary.get("rr_base_label") or "R:R (от входа)"
        blocks.append(
            f"✅ <b>Активация</b> · {html.escape(sym_label)} · "
            f"{html.escape(rr_label)} <code>{rr}</code>"
        )
    from hunt_core.prizrak.build import build_deep_report as _build_deep_report
    from hunt_core.prizrak.format_telegram import format_deep_analysis_telegram as _fmt_deep

    analysis = _build_deep_report(row, include_watch_appendix=False)
    blocks.append(_fmt_deep(analysis))
    grid = build_confluence_grid(row)
    if grid:
        blocks.extend(["", format_grid_telegram(grid, price=float(row.get('price') or 0))])
    maps_block = format_intraday_maps_telegram(row)
    if maps_block:
        blocks.extend(["", maps_block])
    from hunt_core.prizrak.engines.config import load_analyst_config
    from hunt_core.prizrak.engines.delivery_policy import format_cycle_peers_footer
    from hunt_core.prizrak.engines.signal_queue import format_queue_telegram

    v2cfg = load_analyst_config()
    if cycle_peers:
        peer_block = format_cycle_peers_footer(row, cycle_peers)
        if peer_block:
            blocks.extend(["", peer_block])
    if v2cfg.signal_queue_tg_footer:
        qblock = format_queue_telegram(row.get("signal_queue"))
        if qblock:
            blocks.extend(["", qblock])
    result = await broadcaster.send_html("\n".join(blocks))
    if result.status == "sent":
        LOG.info("analyst_pinned_tg_sent", symbol=sym, message_id=result.message_id, plane="deep")
        return True
    LOG.warning("analyst_pinned_tg_failed", symbol=sym, status=result.status, reason=result.reason)
    return False


def _prizrak_row_variants(row: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
    """Expand a pinned row into one lifecycle variant per Prizrak setup_kind.

    Prizrak produces 0..N independent candidates per tick (``prizrak_signals``);
    each setup_kind (level_core / pp_break / trap_flip / level_intraday_scalp /
    zone_target_deep …) is a distinct thesis and should get its own Telegram
    message — the lifecycle spine dedups by setup_id so re-runs don't spam. Each
    variant is a shallow copy of the row with ``prizrak_summary`` swapped to the
    strongest candidate of that setup_kind. Falls back to the row as-is when
    there are no candidates (single-summary behavior preserved).
    """
    sigs = row.get("prizrak_signals")
    if not isinstance(sigs, list) or len(sigs) <= 1:
        summary = row.get("prizrak_summary") if isinstance(row.get("prizrak_summary"), dict) else {}
        return [(row, str(summary.get("setup_kind") or "deep"))]
    best_by_kind: dict[str, dict[str, Any]] = {}
    for c in sigs:
        if not isinstance(c, dict):
            continue
        kind = str(c.get("setup_kind") or "deep")
        cur = best_by_kind.get(kind)
        if cur is None or float(c.get("strength") or 0) > float(cur.get("strength") or 0):
            best_by_kind[kind] = c
    variants: list[tuple[dict[str, Any], str]] = []
    for kind, cand in best_by_kind.items():
        variant = dict(row)
        variant["prizrak_summary"] = cand
        variants.append((variant, kind))
    return variants


async def analyst_pinned_loop(
    client: HuntCcxtClient,
    broadcaster: Any | None,
    *,
    interval_s: float | None = None,
    send_telegram: bool = True,
    ws_feed: Any | None = None,
) -> None:
    """Background continuous deep analysis for pinned anchors."""
    from hunt_core.runtime.state import should_stop

    import asyncio

    interval = interval_s if interval_s is not None else analyst_pinned_interval_s()
    LOG.info("analyst_pinned_loop_start", symbols=list(PINNED_SYMBOLS), interval_s=interval)
    while not should_stop():
        from hunt_core.prizrak.engines.config import load_analyst_config
        from hunt_core.prizrak.engines.delivery_policy import pick_hero_row
        from hunt_core.prizrak.engines.signal_queue import load_signal_queue
        from hunt_core.runtime.emitter import SignalEmitter

        v2cfg = load_analyst_config()
        emitter = SignalEmitter()
        lifecycle_candidates: list[dict[str, Any]] = []
        for sym in PINNED_SYMBOLS:
            if should_stop():
                break
            try:
                row = await assemble_analyst_tick(sym, client, ws_feed=ws_feed)
                if row.get("error"):
                    LOG.info("analyst_pinned_tick_error", symbol=sym, error=row.get("error"))
                    continue
                # Lifecycle spine is the SOLE emission gate — dedup/cooldown/silence all live in
                # process_lifecycle_tick. No legacy fingerprint pre-gate: it would suppress real
                # forming→activated advances (price entering the zone without a fingerprint flip).
                # A7: one lifecycle candidate per Prizrak setup_kind (independent
                # signals), not just the single best summary.
                for variant, kind in _prizrak_row_variants(row):
                    transition = emitter.preview_deep_row(variant)
                    if transition.event != "none":
                        lifecycle_candidates.append((variant, transition, kind))
            except Exception:
                LOG.exception("analyst_pinned_loop_symbol_failed", symbol=sym)

        if send_telegram and broadcaster is not None and lifecycle_candidates:
            from hunt_core.prizrak.arbiter import deep_cooldown_ok, mark_deep_sent

            queue = load_signal_queue()
            rows_only = [r for r, _, _ in lifecycle_candidates]
            if v2cfg.signal_queue_tg_batch and len(lifecycle_candidates) > 1:
                # Batch mode: collapse to a single hero message (config-controlled,
                # unchanged). Multi-emission (one message per setup_kind) is the
                # non-batch path below.
                hero = pick_hero_row(rows_only, queue)
                to_send = [(hero, tr, k) for r, tr, k in lifecycle_candidates if r is hero] if hero else lifecycle_candidates[:1]
            else:
                to_send = lifecycle_candidates
            for row, transition, kind in to_send:
                sym = str(row.get("symbol") or "").upper()
                # Per-(symbol, setup_kind) cooldown so distinct theses on one
                # symbol each get through, but the same thesis can't spam.
                cooldown_key = f"{sym}:{kind}"
                if deep_cooldown_ok(cooldown_key):
                    if await emitter.emit_deep(
                        broadcaster,
                        row,
                        cycle_peers=rows_only,
                        transition=transition,
                    ):
                        mark_deep_sent(cooldown_key)
        try:
            await asyncio.sleep(max(30.0, interval))
        except asyncio.CancelledError:
            break
    LOG.info("analyst_pinned_loop_stop")


__all__ = [
    "append_deep_tick_jsonl",
    "assemble_analyst_tick",
    "analyst_pinned_interval_s",
    "analyst_pinned_loop",
    "material_deep_change",
]
