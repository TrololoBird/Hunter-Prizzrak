"""Watch main loop — universe, prescan, tick scheduling (Phase 8 split)."""
from __future__ import annotations

import asyncio
import faulthandler
import os
import time
from collections.abc import Sequence
from typing import Any

from hunt_core import clock, serde
from hunt_core.view.runtime import MarketRuntime, build_market_runtime
from hunt_core.data.collect import TickBatchCache, safe_fetch
from hunt_core.data.lake import FeatureLakeWriter, buffer_tick_rows, flush_lake
from hunt_core.scanner.feed import EngineScannerFeed, ScannerFeed
from hunt_core.scanner.prescan import (
    PrescanDebounceQueue,
    PrescanEngine,
    apply_quality_gates,
    prescan_from_tickers,
)
from hunt_core.data.baseline_store import batch_update_baselines
from hunt_core.data.universe import PINNED_SYMBOLS, resolve_watch_universe
from hunt_core.deliver.digest import DigestCandidate, get_digest_scheduler
from hunt_core.deliver.telegram import TelegramBroadcaster
from hunt_core.domain.config import (
    SCAN_INTERVAL_S,
    TICK_ROTATE_INTERVAL_S,
    TICK_ROTATE_MIN_BYTES,
)
from hunt_core.regime.market_regime import (
    REGIME_REFRESH_S,
    apply_snapshot,
    load_regime_file,
    refresh_market_regime,
)
from hunt_core.errors import DEFENSIVE_EXC, defensive_exc_types, system_breakers
from hunt_core.features.prepare import min_required_bars
from hunt_core.market import (
    CrossExchangeConfig,
    HuntLoadPlanner,
    apply_cross_exchange_env,
    create_hunt_market_plane_from_settings,
    fetch_secondary_ticker_overlay,
    gate_symbol_list,
    load_cross_exchange_config,
    refresh_cross_exchange_cache,
)
from hunt_core.params.store import migrate_calibration_split, prescan_thresholds
from hunt_core.runtime.cycle._cycle_tick import run_tick
from hunt_core.runtime.heartbeat import beat as _wd_beat
from hunt_core.runtime.heartbeat import seconds_since_progress as _wd_gap
from hunt_core.data.symbol_blacklist import is_blacklisted
from hunt_core.runtime.state import (
    LOG,
    OUT_PATH,
    SYMBOL_WATCH_MODES,
    new_session_state,
    should_stop,
)
from hunt_core.runtime.telegram_commands import build_hunt_telegram_commands
from hunt_core.runtime.tick_io import rotate_hunt_ticks, rotate_telemetry_jsonl
from hunt_core.track.events import record_funnel_stage
from hunt_core.track.pump_history import (
    backfill_from_jsonl,
    load_pump_history,
    observe_prices,
    save_pump_history,
)
from hunt_core.track.tracker import iter_active_tracker_symbols, load_tracker_state
from hunt_core.domain.config import load_settings
from hunt_core.market.network import detect_local_proxies


_ORPHAN_WS_LOG_STATE: dict[str, float] = {"count": 0.0, "next_emit": 0.0}
_ORPHAN_WS_LOG_INTERVAL_S = 60.0
# Consecutive critical-blackout ticks before a supervised self-restart (auto-recovery
# for a stalled WS plane that the progress watchdog can't see). The alert fires at
# streak≥3, so this leaves ~7 ticks of warning first; never fires on an IP ban.
_BLACKOUT_RESTART_STREAK = int(os.getenv("HUNT_BLACKOUT_RESTART_STREAK", "10"))


def _log_orphan_ws(exc: BaseException) -> None:
    """Rate-limit the orphaned-WS transport error log.

    When ``fstream.binance.com`` is unreachable, ccxt.pro's internal client retries
    the aiohttp connection in a tight loop and every failed attempt surfaces as an
    orphaned future exception routed here. Logging each one unbounded produced the
    700 MB+ ``hunt_live.log`` seen in the field. We emit the first occurrence, then
    at most one summary line per :data:`_ORPHAN_WS_LOG_INTERVAL_S` window carrying the
    suppressed count — no third-party dependency, structlog only.
    """
    now = time.monotonic()
    state = _ORPHAN_WS_LOG_STATE
    state["count"] += 1
    if now < state["next_emit"]:
        return
    suppressed = int(state["count"]) - 1
    if suppressed > 0:
        LOG.debug("asyncio_orphan_ws | %s | repeated_%d_times_suppressed", exc, suppressed)
    else:
        LOG.debug("asyncio_orphan_ws | %s", exc)
    state["count"] = 0.0
    state["next_emit"] = now + _ORPHAN_WS_LOG_INTERVAL_S


def _build_digest_candidates(
    gated_ticker_rows: list[dict[str, Any]],
) -> list[DigestCandidate]:
    """Score gated tickers into pump/dump candidates for the scheduled digest.

    Score = |24h change %| with a mild liquidity weight so a thin-volume mover
    does not outrank a high-volume one at equal magnitude.
    """
    out: list[DigestCandidate] = []
    for row in gated_ticker_rows:
        sym = str(row.get("symbol") or "").strip().upper()
        if not sym:
            continue
        chg_raw = row.get("price_change_percent")
        if chg_raw is None:
            chg_raw = row.get("price_change_pct")
        try:
            chg = float(chg_raw if chg_raw is not None else 0)
        except (TypeError, ValueError):
            continue
        if not chg:
            continue
        try:
            qvol = float(row.get("quote_volume") or row.get("quoteVolume") or 0.0)
        except (TypeError, ValueError):
            qvol = 0.0
        liq_w = 1.0 + min(qvol / 1e8, 1.0) * 0.25
        out.append(
            DigestCandidate(
                symbol=sym,
                direction="pump" if chg > 0 else "dump",
                score=abs(chg) * liq_w,
                change_24h_pct=chg,
            )
        )
    return out


async def _manipulation_scan_loop(
    cli_symbols: Sequence[str],
    feed: ScannerFeed,
    broadcaster: Any | None,
    send_telegram: bool,
    *,
    interval_s: int = 300,
) -> None:
    """Periodic scan for manipulation reversal setups (scanner/detect/patterns.py).

    Detects Pattern A (long: impulse→absorption→bokovik→sweep→break) and
    Pattern B (short: HTF sweep→fade→LTF_confirm) across the non-pinned
    universe. Each scan fetches OHLCV for all tracked symbols, runs the
    state machine, and delivers if score ≥ 0.50.

    The universe is re-resolved EVERY cycle. It used to be a list captured once at
    process start, so the watchlist that prescan keeps rewriting never reached the
    scanner: a coin that started coiling after boot was invisible until the next
    restart — and the whole point of the prescan is to surface exactly those.
    """
    from hunt_core.deliver.manipulation_delivery import deliver_manipulation_setups
    from hunt_core.data.lake import buffer_tracker_state, flush_tracker_state
    from hunt_core.data.universe import PINNED_SYMBOLS, load_watchlist_symbols
    from hunt_core.track.tracker import load_tracker_state

    LOG.info("manipulation_scan_loop_started interval=%s", interval_s)
    while not should_stop():
        try:
            # Pinned symbols are Prizrak's exclusive domain; scanner owns the rest.
            # Blacklisted symbols are skipped like the tick loop does: without
            # this a symbol blacklisted mid-session kept costing 6 TFs of REST +
            # funding every cycle, failing at debug level and burning weight.
            pinned_upper = {str(s).upper() for s in PINNED_SYMBOLS}
            symbols = [
                s
                for s in dict.fromkeys(list(cli_symbols) + load_watchlist_symbols())
                if str(s).upper() not in pinned_upper and not is_blacklisted(s)
            ]
            cycle_started = time.monotonic()
            delivered: list[dict[str, Any]] = []
            if send_telegram and broadcaster is not None and symbols:
                ts = load_tracker_state()
                delivered = await deliver_manipulation_setups(
                    symbols, feed, broadcaster, tracker_state=ts
                ) or []
                buffer_tracker_state(ts)
                flush_tracker_state()
            # One line per cycle. The loop used to log ONLY on delivery, so a
            # quiet scanner was indistinguishable from a broken one: 2h of live
            # silence could not be attributed without reading the source.
            LOG.info(
                "manipulation_scan_cycle",
                symbols=len(symbols),
                delivered=len(delivered),
                delivered_syms=[str(d.get("symbol")) for d in delivered][:5],
                duration_s=round(time.monotonic() - cycle_started, 1),
                telegram=bool(send_telegram and broadcaster is not None),
            )
        except asyncio.CancelledError:
            break
        except Exception:
            LOG.exception("manipulation_scan_loop_error")
        await asyncio.sleep(interval_s)

    LOG.info("manipulation_scan_loop_stopped")


def _engine_universe(*symbol_groups: Sequence[str]) -> tuple[list[str], list[str]]:
    """Binance-id / short symbols → unified ccxt (futures, spot) for the engine (USDT-linear only).

    ``"BTCUSDT"``/``"BTC"``/``"BTC/USDT:USDT"`` → ``("BTC/USDT:USDT", "BTC/USDT")``. De-duplicated,
    order-preserving. Used only by the coexistence engine wiring below.
    """
    fut: list[str] = []
    spot: list[str] = []
    for group in symbol_groups:
        for raw in group:
            s = str(raw).upper().replace("/", "").replace(":USDT", "")
            if not s.endswith("USDT"):
                s = f"{s}USDT"
            base = s[:-4]
            if base:
                fut.append(f"{base}/USDT:USDT")
                spot.append(f"{base}/USDT")
    return list(dict.fromkeys(fut)), list(dict.fromkeys(spot))


async def run_loop(
    cli_symbols: tuple[str, ...],
    interval_s: int,
    once: bool,
    *,
    send_telegram: bool,
) -> None:

    from hunt_core.runtime.cycle import _impl as _loop_impl

    _overlay_ws_tickers = _loop_impl._overlay_ws_tickers
    _TICK_LOCK = _loop_impl._TICK_LOCK

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _prev_loop_handler = asyncio.get_running_loop().get_exception_handler()

    def _hunt_loop_exc_handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        exc = context.get("exception")
        if exc is not None:
            from hunt_core.market import HuntCcxtStreams

            if HuntCcxtStreams._ws_transport_fatal(exc):
                _log_orphan_ws(exc)
                return
        if _prev_loop_handler is not None:
            _prev_loop_handler(loop, context)
        else:
            loop.default_exception_handler(context)

    asyncio.get_running_loop().set_exception_handler(_hunt_loop_exc_handler)
    if migrate_calibration_split():
        LOG.info("hunt_calibration_migrated", path="hunt/data/hunt_calibration.json")
    try:
        from hunt_core.params.store import invalidate_calibration_cache

        invalidate_calibration_cache()
        LOG.debug("hunt_calibration_rebuild_skipped", reason="module_unavailable")
    except Exception:
        LOG.exception("hunt_calibration_rebuild_failed")
    try:
        rot_stats = rotate_hunt_ticks()
        if rot_stats.get("appended_lines") or rot_stats.get("archived"):
            LOG.info("hunt_tick_rotate", **rot_stats)
        tel_stats = rotate_telemetry_jsonl()
        if tel_stats.get("rotated"):
            LOG.info("hunt_telemetry_rotate", **tel_stats)
    except Exception:
        LOG.exception("hunt_tick_rotate_failed")
    settings = load_settings()
    tg_proxies = await detect_local_proxies() if send_telegram else []
    proxy_url: str | None = tg_proxies[0] if tg_proxies else None
    if proxy_url:
        LOG.info("watch_telegram_proxy", proxy=proxy_url)
    broadcaster: TelegramBroadcaster | None = None
    if send_telegram:
        if not settings.tg_token or not settings.target_chat_id:
            LOG.warning(
                "watch_telegram_disabled",
                reason="missing_credentials",
                missing=[
                    "TELEGRAM_BOT_TOKEN" if not settings.tg_token else None,
                    "TELEGRAM_CHAT_ID" if not settings.target_chat_id else None,
                ],
            )
            send_telegram = False
        else:
            for attempt in range(3):
                try:
                    broadcaster = TelegramBroadcaster(
                        settings.tg_token, settings.target_chat_id, proxy_url=proxy_url,
                    )
                    await broadcaster.preflight_check()
                    LOG.info("watch_telegram_ready", chat=settings.target_chat_id, mode="confirm_only")
                    break
                except DEFENSIVE_EXC as exc:
                    LOG.warning("watch_telegram_preflight_failed", attempt=attempt + 1, error=repr(exc))
                    broadcaster = None
                    if attempt < 2:
                        await asyncio.sleep(2.0 * (attempt + 1))
            if broadcaster is None:
                LOG.warning("watch_telegram_disabled", reason="preflight_failed")
                send_telegram = False

    minimums = min_required_bars(
        min_bars_15m=settings.filters.min_bars_15m,
        min_bars_1h=settings.filters.min_bars_1h,
        min_bars_4h=settings.filters.min_bars_4h,
    )
    cross_cfg: CrossExchangeConfig = load_cross_exchange_config()
    apply_cross_exchange_env(cross_cfg)
    LOG.info(
        "hunt_multi_exchange",
        enabled=cross_cfg.enabled,
        ws=cross_cfg.ws_enabled,
        exchanges=",".join(cross_cfg.exchanges),
        refresh_s=cross_cfg.refresh_interval_s,
        max_symbols=cross_cfg.max_symbols_per_refresh,
    )
    # Startup network/DNS can be transiently down (e.g. right after the host wakes
    # from sleep). Binance is reached directly (no proxy pool), so a single blip is
    # handled by retrying the full plane creation a few times with backoff before
    # actually failing startup.
    plane = None
    _plane_exc: Exception | None = None
    market_runtime: MarketRuntime | None = None  # ADR-0004 coexistence engine (gated, see below)
    for _attempt in range(1, 4):
        try:
            plane = await create_hunt_market_plane_from_settings(settings)
            break
        except Exception as exc:
            _plane_exc = exc
            LOG.warning(
                "hunt_market_plane_startup_retry | attempt=%d error=%s",
                _attempt, type(exc).__name__,
            )
            if _attempt < 3:
                await asyncio.sleep(20.0 * _attempt)
    if plane is None:
        assert _plane_exc is not None  # set on every failed attempt above
        raise _plane_exc
    client = plane.client
    ws_feed = plane.streams
    spot_companion = plane.spot
    # Expose the live spot companion so the deep/analyst plane (assemble_analyst_tick)
    # can reuse the same spot exchange + weight budget for its per-symbol enrichment.
    from hunt_core.runtime.tick_state import (
        set_live_market_runtime,
        set_live_spot_companion,
        set_live_spot_engine,
    )

    set_live_spot_companion(spot_companion)

    # ── ADR-0004: the engine-native MarketRuntime is the transport for the scanner (and, as the
    # tick-swap lands, the deep/main tick). Constructed for the continuous loop — the ccxt.pro engine
    # (MultiEngine + SpotEngine) over the pinned universe — alongside the still-live legacy plane
    # during the cutover. Gated on `not once` for now: only the continuous scanner uses it today, so a
    # one-shot smoke stays legacy-only (un-gate when the main tick swaps onto the view). A start
    # failure degrades (logged, market_runtime=None) → the scanner is skipped, never sinks the loop.
    if not once:
        try:
            eng_fut, eng_spot = _engine_universe(PINNED_SYMBOLS, cli_symbols)
            market_runtime = build_market_runtime(eng_fut, spot_symbols=eng_spot)
            await market_runtime.start()
            set_live_spot_engine(market_runtime.spot)
            set_live_market_runtime(market_runtime)
            LOG.info("engine_runtime_started", futures=len(eng_fut), spot=len(eng_spot))
        except Exception:
            LOG.exception("engine_runtime_start_failed")  # degrade, never sink the loop
            market_runtime = None
            set_live_spot_engine(None)
            set_live_market_runtime(None)

    # ── exchange health check ──────────────────────────────────
    try:
        st = await client.fetch_status()
        if st:
            status = str(st.get("status") or "")
            if status and status not in ("ok", "open"):
                LOG.warning(
                    "hunt_exchange_status_unexpected | status=%s info=%s",
                    status,
                    str(st.get("info", {}))[:200],
                )
    except Exception:
        LOG.exception("hunt_exchange_status_check_failed")
    ws_feed.set_symbols(list(cli_symbols))
    await ws_feed.start()
    # Persistent across ticks: kline/OI caches live in client; oi_flush/oi_build need prev tick.
    prev_oi: dict[str, float | None] = {}
    last_bias: dict[str, str] = {}
    last_lifecycle_phase: dict[str, str] = {}
    symbol_state = new_session_state()

    manipulation_task: asyncio.Task[None] | None = None
    if not once:
        # ADR-0004 S7: the scanner reads its detection frames off the ENGINE — EngineScannerFeed on
        # the primary engine's ccxt client (engine.exchange + engine.rest, the on-demand REST tail the
        # engine serves for non-tracked symbols). The legacy client feed is deleted; if the engine
        # runtime failed to start the scanner is simply skipped (logged), never client-fed.
        if market_runtime is None:
            LOG.error("manipulation_scan_disabled | engine runtime unavailable")
        else:
            scanner_feed: ScannerFeed = EngineScannerFeed(market_runtime.multi.primary)
            # Pass the CLI seed only — the loop re-resolves watchlist ∪ cli minus pinned on
            # every pass, so freshly-prescanned coins are actually scanned (see docstring).
            manipulation_task = asyncio.create_task(
                _manipulation_scan_loop(cli_symbols, scanner_feed, broadcaster, send_telegram),
                name="manipulation_scan_loop",
            )
            LOG.info("manipulation_scan_loop_scheduled", engine_fed=True)

    from hunt_core.data.frame_cache import reset_frame_cache

    reset_frame_cache()
    feature_lake = FeatureLakeWriter()
    prescan_debounce = PrescanDebounceQueue(
        debounce_s=float(
            os.getenv(
                "HUNT_PRESCAN_DEBOUNCE_S",
                str(prescan_thresholds()["debounce_s"]),
            )
            or prescan_thresholds()["debounce_s"]
        ),
    )
    prescan_engine = PrescanEngine()
    load_planner = HuntLoadPlanner()
    digest_scheduler = get_digest_scheduler()
    _lake_warmed_syms: set[str] = set()
    pump_store = load_pump_history()
    if not pump_store.symbols and not pump_store.event_log:
        backfill_from_jsonl(pump_store)
        save_pump_history(pump_store)

    # --once smoke: skip heavy first-tick scan/cross-ex (full watchlist prescan).
    _now_mono = time.monotonic()
    last_scan = _now_mono if once else 0.0
    last_regime = _now_mono if once else 0.0
    last_cross_ex = _now_mono if once else 0.0
    _cross_ex_cache: dict[str, dict[str, Any]] = {}
    # P1.8: secondary-CEX 24h ticker overlay for the prescan outlier matrix.
    _secondary_ticker_overlay: dict[str, dict[str, Any]] = {}
    last_secondary_tickers = 0.0
    last_tick_rotate = time.monotonic()
    batch_cache = TickBatchCache()
    cached = load_regime_file()
    if cached is not None:
        apply_snapshot(cached)
    if not once:
        try:
            await refresh_market_regime(client)
            last_regime = time.monotonic()
        except Exception:
            LOG.exception("market_regime_startup_failed")
    elif cached is not None:
        LOG.info("watch_once_regime_cached", regime=getattr(cached, "regime", None))

    _startup_tg = os.getenv("HUNT_STARTUP_TELEGRAM", "1").strip().lower()
    from hunt_core.paths import SESSION_DIR

    startup_sentinel = SESSION_DIR / "startup_telegram.sent"
    cold_start = not startup_sentinel.exists()
    if (
        broadcaster is not None
        and send_telegram
        and not once
        and cold_start
        and _startup_tg not in {"0", "false", "no"}
    ):
        cross_line = ", ".join(cross_cfg.exchanges) if cross_cfg.enabled else "off"
        try:
            await broadcaster.send_html(
                "🟢 <b>Hunt live</b>\n"
                f"Interval {interval_s}s · confirm-only alerts\n"
                f"Cross-intel: {cross_line}\n"
                "<i>Не auto-trade</i>"
            )
            startup_sentinel.parent.mkdir(parents=True, exist_ok=True)
            startup_sentinel.write_text(clock.now_utc().isoformat(), encoding="utf-8")
            LOG.info("watch_startup_telegram_sent", chat=settings.target_chat_id, cold_start=True)
        except Exception:
            LOG.exception("watch_startup_telegram_failed")

    # /signal polling conflicts with a second getUpdates consumer — only when TG sends enabled.
    tg_cmds = (
        build_hunt_telegram_commands(settings, proxy_url=proxy_url, client=client)
        if send_telegram and settings.tg_token
        else None
    )
    tg_task: asyncio.Task[None] | None = None
    if tg_cmds is not None:
        tg_task = asyncio.create_task(tg_cmds.run_forever(), name="hunt_tg_commands")
        LOG.info("hunt_telegram_commands_scheduled")

    deep_task: asyncio.Task[None] | None = None
    if not once and os.getenv("HUNT_DEEP_PINNED_LOOP", "1").strip().lower() not in {"0", "false", "no"}:
        from hunt_core.runtime.analyst_assembly import analyst_pinned_loop

        deep_task = asyncio.create_task(
            analyst_pinned_loop(client, broadcaster, send_telegram=send_telegram, ws_feed=ws_feed),
            name="analyst_pinned_loop",
        )
        LOG.info("analyst_pinned_loop_scheduled")

    path_backfill_task: asyncio.Task[None] | None = None
    if not once:
        from hunt_core.track.path_backfill import path_backfill_loop

        path_backfill_task = asyncio.create_task(
            path_backfill_loop(client, interval_s=900.0),
            name="path_backfill_loop",
        )
        LOG.info("path_backfill_scheduled", interval_s=900.0)

    # Hang watchdog: if a cycle stalls (e.g. an unbounded loop in scan/levels on
    # degenerate data), faulthandler dumps every Python thread's stack — it works
    # even while the GIL is held by a tight loop — then hard-exits so the process
    # stops being a frozen zombie and can be restarted.
    faulthandler.enable()
    _wd_timeout_s = float(os.getenv("HUNT_WATCHDOG_S", "300") or 300)
    _wd_file = (OUT_PATH.parent / "hunt_watchdog.log").open("a", buffering=1)
    LOG.info("hunt_watchdog_armed", timeout_s=_wd_timeout_s, mode="progress_heartbeat")

    async def _watchdog_rearmer() -> None:
        # Progress-driven hang watchdog. The old design armed a fixed per-tick 300s
        # faulthandler deadline over the WHOLE tick body — so a tick that was merely SLOW
        # (the REST weight pacer legitimately sleeping 12-21s per call to stay under Binance's
        # limit) was killed exactly like a hang. Instead, push faulthandler's C-timer forward
        # while work is advancing: the cycle and the REST pacer call ``heartbeat.beat()`` (a
        # rate-limit sleep IS progress), and we re-arm the timer to ``timeout - seconds_since_
        # progress`` each check. It fires ONLY after a genuine no-progress stall for the full
        # timeout — including a GIL-held tight loop, which stops the re-arms so the C-timer
        # (independent of the event loop) elapses and dumps every thread's stack.
        check_s = max(1.0, min(5.0, _wd_timeout_s / 20.0))
        while True:
            remaining = _wd_timeout_s - _wd_gap()
            faulthandler.cancel_dump_traceback_later()
            if remaining <= 0.0:
                LOG.critical("hunt_watchdog_no_progress", timeout_s=_wd_timeout_s)
                faulthandler.dump_traceback(file=_wd_file)
                os._exit(1)
            faulthandler.dump_traceback_later(remaining, repeat=False, file=_wd_file, exit=True)
            await asyncio.sleep(check_s)

    _wd_task = asyncio.create_task(_watchdog_rearmer()) if not once else None
    _pinned_brief_sent = False
    _last_checkpoint = time.monotonic()
    _last_htf_persist = time.monotonic()
    # Reload persisted HTF (1h/4h/1d) frames so a restart has a fresh-enough
    # fallback instead of a stale bootstrap seed — collapses the post-restart
    # HTF-staleness blackout while REST backfill catches up. Best effort.
    try:
        from hunt_core.data.frame_cache import get_frame_cache as _gfc
        from hunt_core.paths import HTF_FRAMES as _HTF_FRAMES

        _n_htf = _gfc().load_htf_frames(_HTF_FRAMES)
        if _n_htf:
            LOG.info("htf_frames_reloaded", frames=_n_htf)
    except Exception:
        LOG.exception("htf_frames_reload_failed")
    _degraded_streak = 0  # consecutive ticks the whole universe failed data assembly
    try:
        tick_ctx: dict[str, Any] | None = None
        while not should_stop():
            started = time.monotonic()
            try:
                if not once and time.monotonic() - last_regime >= REGIME_REFRESH_S:
                    try:
                        snap = await refresh_market_regime(client)
                        last_regime = time.monotonic()
                        LOG.info(
                            "market_regime_tick",
                            regime=snap.regime,
                            anomaly_chg=snap.params.anomaly_min_chg_24h_pct,
                            n_liquid=snap.n_liquid,
                        )
                    except Exception:
                        LOG.exception("market_regime_refresh_failed")
                        last_regime = time.monotonic()

                if not once and time.monotonic() - last_scan >= SCAN_INTERVAL_S:
                    try:
                        from hunt_core.params.store import hunter_thresholds
                        from hunt_core.scanner.prescan import run_scan

                        _ht = hunter_thresholds()
                        summary = await run_scan(
                            limit=int(_ht.get("watchlist_limit", 50)),
                            min_score=float(_ht.get("score_watch", 45.0)),
                            client=client,
                        )
                        LOG.info(
                            "hunt_scan_refresh",
                            watch=summary.get("watch_count"),
                            priority=summary.get("priority_count"),
                        )
                    except defensive_exc_types(asyncio.IncompleteReadError) as exc:
                        LOG.warning("hunt_scan_refresh_failed", error=repr(exc))
                    last_scan = time.monotonic()

                settings = load_settings()
                now = clock.now_utc()
                ticker_raw = await asyncio.wait_for(
                    safe_fetch(
                        client.fetch_ticker_24h,
                        context="ticker_24h",
                        client=client,
                    ),
                    timeout=120.0,
                ) or []
                ticker_by_sym = {str(t.get("symbol")): t for t in ticker_raw if t.get("symbol")}
                ex = client.exchange
                # P1.6: prescan outliers feed an internal debounce queue, NOT
                # Telegram. Ready (debounced) symbols merge into the watch universe.
                gated_ticker_rows = [
                    t for t in ticker_raw if apply_quality_gates(t)[0]
                ]
                # P1.8: refresh secondary-CEX ticker overlay on the cross-ex cadence
                # (soft — a stale/empty overlay leaves prescan on primary only).
                if (
                    not once
                    and cross_cfg.enabled
                    and len(gated_ticker_rows) <= 100
                    and (
                        not _secondary_ticker_overlay
                        or time.monotonic() - last_secondary_tickers
                        >= cross_cfg.refresh_interval_s
                    )
                ):
                    try:
                        _secondary_ticker_overlay = await fetch_secondary_ticker_overlay(
                            client, cfg=cross_cfg
                        )
                    except Exception:
                        LOG.exception("secondary_ticker_overlay_refresh_failed")
                    last_secondary_tickers = time.monotonic()
                # P1.10: primary OI % change overlay (cached ratio → percent; None
                # when unseen, so divergence stays soft).
                _oi_change_by_sym: dict[str, float | None] = {}
                for _t in gated_ticker_rows:
                    _sym = str(_t.get("symbol") or "")
                    if not _sym:
                        continue
                    _ratio = client.get_cached_oi_change(_sym)
                    _oi_change_by_sym[_sym] = (
                        _ratio * 100.0 if _ratio is not None else None
                    )
                batch_update_baselines(gated_ticker_rows, oi_by_sym=_oi_change_by_sym)
                _prescan_hits = prescan_from_tickers(
                    gated_ticker_rows,
                    engine=prescan_engine,
                    secondary_overlay=_secondary_ticker_overlay,
                    oi_change_by_sym=_oi_change_by_sym,
                )
                prescan_debounce.offer(_prescan_hits)
                # P1.17: strongest outlier per symbol for the early-advisory merge.
                prescan_outlier_by_sym: dict[str, dict[str, Any]] = {}
                for _h in _prescan_hits:
                    prev = prescan_outlier_by_sym.get(_h.symbol)
                    if prev is None or _h.energy > prev.get("energy", 0.0):
                        prescan_outlier_by_sym[_h.symbol] = {
                            "direction": _h.direction,
                            "change_pct": _h.change_pct,
                            "energy": _h.energy,
                            "readiness_direction": _h.readiness_direction,
                            "interval": _h.interval,
                            "cross_venues": _h.cross_venues,
                            "oi_divergence": getattr(_h, "oi_divergence", None),
                        }
                prescan_ready = prescan_debounce.drain_ready()
                if prescan_ready:
                    LOG.info(
                        "hunt_prescan_debounce_ready",
                        count=len(prescan_ready),
                        head=[d.symbol for d in prescan_ready[:6]],
                    )
                    try:
                        from hunt_core.diagnostics.universe_audit import (
                            append_prescan_universe_audit,
                        )

                        for _d in prescan_ready:
                            append_prescan_universe_audit(_d, ts=now)
                    except Exception:
                        LOG.exception("hunt_prescan_universe_audit_failed")
                    for d in prescan_ready[:12]:
                        record_funnel_stage(
                            "prescan",
                            symbol=d.symbol,
                            direction=d.direction,
                            detail=f"{d.interval}:{d.change_pct:.1f}%",
                        )
                price_map = {
                    sym: float(row.get("last_price") or 0)
                    for sym, row in ticker_by_sym.items()
                    if float(row.get("last_price") or 0) > 0
                }
                observe_prices(pump_store, price_map, now=now)
                if once:
                    merged = list(dict.fromkeys(s.upper() for s in cli_symbols))
                    mode_map = {
                        s: SYMBOL_WATCH_MODES.get(s, "short") for s in merged
                    }
                else:
                    full_symbols, mode_map = resolve_watch_universe(
                        settings,
                        static_modes=SYMBOL_WATCH_MODES,
                    )
                    merged = list(full_symbols)
                    for sym in cli_symbols:
                        s = sym.upper()
                        if s not in merged:
                            merged.append(s)
                        mode_map.setdefault(s, SYMBOL_WATCH_MODES.get(s, "short"))
                    # P1.6 merge: debounced prescan outliers join the ignition path.
                    prescan_merge_cap = int(
                        os.getenv(
                            "HUNT_PRESCAN_MERGE_CAP",
                            str(prescan_thresholds()["merge_cap"]),
                        )
                        or prescan_thresholds()["merge_cap"]
                    )
                    max_chg_merge = float(
                        os.getenv(
                            "HUNT_PRESCAN_MAX_CHANGE_PCT",
                            str(prescan_thresholds()["max_change_pct_for_merge"]),
                        )
                        or prescan_thresholds()["max_change_pct_for_merge"]
                    )
                    from hunt_core.scanner.prescan import prescan_merge_eligible

                    prescan_filtered: list[Any] = []
                    prescan_skipped_late = 0
                    for _d in prescan_ready:
                        if prescan_merge_eligible(_d, max_change_pct=max_chg_merge):
                            prescan_filtered.append(_d)
                        else:
                            prescan_skipped_late += 1
                            try:
                                from hunt_core.diagnostics.universe_audit import (
                                    append_prescan_merge_skip_audit,
                                )

                                append_prescan_merge_skip_audit(
                                    _d,
                                    reason="late_chase",
                                    max_change_pct=max_chg_merge,
                                    ts=now,
                                )
                            except Exception:
                                LOG.exception("hunt_prescan_merge_skip_audit_failed")
                    if prescan_skipped_late:
                        LOG.info(
                            "hunt_prescan_late_chase_skipped",
                            skipped=prescan_skipped_late,
                            max_change_pct=max_chg_merge,
                            eligible=len(prescan_filtered),
                        )
                    prescan_to_merge = prescan_filtered[: max(prescan_merge_cap, 0)]
                    if len(prescan_ready) > len(prescan_to_merge):
                        LOG.info(
                            "hunt_prescan_merge_capped",
                            ready=len(prescan_ready),
                            merged=len(prescan_to_merge),
                            cap=prescan_merge_cap,
                        )
                    for d in prescan_to_merge:
                        s = d.symbol.upper()
                        if s not in merged:
                            merged.append(s)
                        mode_map.setdefault(
                            s, "short" if d.direction in {"dump", "bear"} else "long"
                        )
                    # Keep open tracker positions in every tick batch — otherwise
                    # SL/TP followups stall until orphan kline reconcile.
                    tracker_pin = load_tracker_state()
                    pinned_n = 0
                    for sym, direction in iter_active_tracker_symbols(tracker_pin):
                        if sym not in merged:
                            merged.append(sym)
                            pinned_n += 1
                        mode_map.setdefault(
                            sym, "short" if direction == "short" else "long"
                        )
                    if pinned_n:
                        LOG.info("watch_tracker_pin", symbols=pinned_n)
                merged = gate_symbol_list(merged, exchange=ex, label="watch_universe")
                active = tuple(dict.fromkeys(merged))
                # Warm the feature lake for any symbol that has never been backfilled.
                # Needed so the phase classifier (requires close × 30+) does not return
                # NEUTRAL on every tick for freshly-promoted scanner candidates.
                _cold = [
                    s for s in active
                    if s not in _lake_warmed_syms
                ]
                if _cold:
                    from hunt_core.data.lake import query_features as _qf
                    from hunt_core.data.lake_warmup import ensure_lake_warm
                    _really_cold = [
                        s for s in _cold
                        if _qf(s, tf="15m", limit=31).height < 30
                    ]
                    if _really_cold:
                        LOG.info("lake_warmup_start", symbols=_really_cold)
                        _warmup_writer = FeatureLakeWriter()
                        await ensure_lake_warm(client, _really_cold, writer=_warmup_writer)
                        _warmup_writer.close()
                    _lake_warmed_syms.update(_cold)
                active = tuple(s for s in active if not is_blacklisted(s))
                hunt_active = tuple(active)
                load_plan = load_planner.plan_tick(
                    hunt_active,
                    interval_s=float(interval_s),
                )
                if load_plan.dropped_symbols:
                    # Demand shaping (ADR-0001 pillar 3): the plan shed these to fit
                    # the weight budget — exclude them from THIS tick's REST snapshots.
                    # WS subscriptions below still cover the full universe (weight-free),
                    # and the keep-window rotates so they return on later ticks.
                    _shed = frozenset(load_plan.dropped_symbols)
                    hunt_active = tuple(s for s in hunt_active if s not in _shed)
                    LOG.info(
                        "hunt_demand_shaped",
                        kept=len(hunt_active),
                        dropped=len(_shed),
                        dropped_list=sorted(_shed)[:10],
                    )
                LOG.info(
                    "hunt_load_plan",
                    symbols=len(hunt_active),
                    ws_symbols=len(active),
                    parallel=load_plan.parallel,
                    full=load_plan.full_count,
                    fast=load_plan.fast_count,
                    est_weight=load_plan.estimated_binance_weight,
                    est_fapi=load_plan.estimated_fapi_calls,
                    cross_max=load_plan.cross_max_symbols,
                    skip_secondary=load_plan.skip_secondary_tickers,
                )
                _overlay_ws_tickers(ticker_by_sym, active, ws_feed)
                ws_feed.set_symbols(
                    list(active),
                    priority=list(cli_symbols),
                )
                ws_n = min(len(active), 24) + 1
                if ws_feed.kline_ws_enabled:
                    ws_n += min(len(active), 24)
                LOG.info(
                    "watch_universe",
                    symbols=len(hunt_active),
                    ws_symbols=len(active),
                    ws_streams=ws_n,
                    kline_ws=ws_feed.kline_ws_enabled,
                    kline_interval="1m",
                    list=list(active)[:8],
                )

                if (
                    not once
                    and cross_cfg.enabled
                    and (
                        not _cross_ex_cache
                        or time.monotonic() - last_cross_ex >= cross_cfg.refresh_interval_s
                    )
                ):
                    try:
                        from dataclasses import replace

                        cross_cfg_tick = replace(
                            cross_cfg,
                            max_symbols_per_refresh=load_plan.cross_max_symbols,
                        )
                        await refresh_cross_exchange_cache(
                            client,
                            active,
                            _cross_ex_cache,
                            cfg=cross_cfg_tick,
                        )
                    except Exception:
                        LOG.exception("cross_exchange_refresh_failed")
                    last_cross_ex = time.monotonic()

                tick_ctx = {
                    "active": active,
                    "settings": settings,
                    "minimums": minimums,
                    "client": client,
                    "prev_oi": prev_oi,
                    "last_bias": last_bias,
                    "last_lifecycle_phase": last_lifecycle_phase,
                    "mode_map": mode_map,
                    "broadcaster": broadcaster,
                    "send_telegram": send_telegram,
                    "ticker_by_sym": ticker_by_sym,
                    "pump_store": pump_store,
                    "ws_feed": ws_feed,
                    "spot_companion": spot_companion,
                    "batch_cache": batch_cache,
                    "tier": "full",
                    "tier_by_symbol": load_plan.tier_by_symbol,
                    "snapshot_parallel": load_plan.parallel,
                    "cross_ex_cache": _cross_ex_cache,
                    "prescan_outlier_by_sym": prescan_outlier_by_sym,
                    "symbol_state": symbol_state,
                    "feature_lake": feature_lake,
                }
                _wd_beat()  # tick start — the watchdog re-armer is progress-driven now
                # Circuit breaker telemetry — log OPEN state once per tick.
                _breakers = system_breakers()
                if not _breakers.rest.can_execute():
                    LOG.warning(
                        "circuit_breaker_rest_open | state=%s failures=%d threshold=%d recovery=%.0fs",
                        _breakers.rest.state.name,
                        _breakers.rest.failures,
                        _breakers.rest.failure_threshold,
                        _breakers.rest.recovery_timeout,
                    )
                if not _breakers.ws.can_execute():
                    LOG.warning(
                        "circuit_breaker_ws_open | state=%s failures=%d threshold=%d recovery=%.0fs",
                        _breakers.ws.state.name,
                        _breakers.ws.failures,
                        _breakers.ws.failure_threshold,
                        _breakers.ws.recovery_timeout,
                    )
                if not _breakers.execution.can_execute():
                    LOG.warning(
                        "circuit_breaker_execution_open | state=%s failures=%d threshold=%d",
                        _breakers.execution.state.name,
                        _breakers.execution.failures,
                        _breakers.execution.failure_threshold,
                    )
                from hunt_core.runtime import telemetry

                async with _TICK_LOCK:
                    with telemetry.span(
                        "cycle.tick",
                        **{
                            "hunt.active_symbols": len(hunt_active),
                            "hunt.send_telegram": send_telegram,
                        },
                    ):
                        rows = await run_tick(
                            hunt_active,
                            **{k: v for k, v in tick_ctx.items() if k != "active"},
                        )
                        telemetry.set_attributes({"hunt.rows_emitted": len(rows or [])})
                _wd_beat()  # tick body completed — mark progress
                # ── universe data-plane health ─────────────────────────────
                # Turn a SILENT mass data blackout (dead proxy → every symbol fails
                # the staleness gate, no signal can form) into a loud, escalating
                # signal instead of letting it run until the watchdog hard-kills a
                # hung loop hours later (2026-07-11 incident).
                if not once and rows:
                    from hunt_core.diagnostics.universe_health import (
                        assess_universe_health,
                        should_self_restart_on_blackout,
                    )

                    _health = assess_universe_health(rows)
                    if _health.degraded:
                        _degraded_streak += 1
                        LOG.warning(
                            "hunt_universe_degraded",
                            streak=_degraded_streak,
                            **_health.telemetry(),
                        )
                        # IP-ban detection — computed once, used by BOTH the alert
                        # (cause hint) and the self-restart guard (never restart on a
                        # ban: it self-heals, and a respawn just re-hits the banned IP).
                        _guard = getattr(getattr(client, "rest_gate", None), "guard", None)
                        try:
                            _ban_pause = _guard.remaining_pause_s() if _guard is not None else 0.0
                        except Exception:
                            _ban_pause = 0.0
                        _last_kind = getattr(getattr(_guard, "telemetry", None), "last_kind", None)
                        _is_ban = _ban_pause > 0 or _last_kind == "ip_ban"
                        # Escalate to an ops alert once the blackout persists (not a
                        # one-off blip) — near-total failure across several ticks.
                        if (
                            _health.critical
                            and _degraded_streak >= 3
                            and send_telegram
                            and broadcaster is not None
                        ):
                            # Cause-aware guidance: the rotating proxy pool was REMOVED
                            # (direct Binance connection), so the old "проверьте прокси"
                            # text is stale and misleading. The dominant real cause of a
                            # klines blackout is a Binance IP rate-limit ban (418/429)
                            # that pauses the REST plane and starves the 4h refresh —
                            # detect it and say so (it self-heals when the ban lifts).
                            if _is_ban:
                                _cause_hint = (
                                    f"⏳ Binance IP-бан/rate-limit — REST на паузе (~{_ban_pause:.0f}s). "
                                    "Частота запросов уже снижена; ждём снятия, процесс восстановится сам."
                                )
                            else:
                                _cause_hint = (
                                    "Проверьте доступ к Binance (соединение прямое, без прокси) "
                                    "или зависший фетч — сигналы не формируются."
                                )
                            try:
                                await broadcaster.send_html(
                                    "🚨 <b>Data blackout</b>: "
                                    f"{_health.failures}/{_health.total} символов "
                                    f"({_health.failure_frac * 100:.0f}%) не проходят проверку "
                                    f"данных {_degraded_streak} тиков подряд.\n"
                                    f"Причина: <code>{_health.dominant_kind}</code>.\n"
                                    f"{_cause_hint}"
                                )
                            except Exception:
                                LOG.exception("hunt_universe_degraded_alert_failed")
                        # AUTO-RECOVERY: a sustained critical NON-ban blackout (e.g. a
                        # stalled WS mux — 2026-07-13) doesn't trip the progress watchdog,
                        # so recover by exiting for a clean supervised respawn. HTF frames
                        # are persisted first (os._exit skips `finally`) so the respawn has
                        # no warmup blackout.
                        if should_self_restart_on_blackout(
                            critical=_health.critical,
                            degraded_streak=_degraded_streak,
                            supervised=os.getenv("HUNT_WATCH_SUPERVISE", "0").strip().lower()
                            in {"1", "true", "yes"},
                            is_ban=_is_ban,
                            streak_threshold=_BLACKOUT_RESTART_STREAK,
                        ):
                            LOG.critical(
                                "hunt_data_blackout_self_restart",
                                streak=_degraded_streak,
                                **_health.telemetry(),
                            )
                            try:
                                from hunt_core.data.frame_cache import get_frame_cache as _gfc
                                from hunt_core.paths import HTF_FRAMES as _HTF

                                _gfc().persist_htf_frames(_HTF)
                            except Exception:
                                LOG.exception("blackout_restart_persist_failed")
                            os._exit(1)
                    else:
                        _degraded_streak = 0
                if (
                    not once
                    and not _pinned_brief_sent
                    and send_telegram
                    and broadcaster is not None
                ):
                    from hunt_core.runtime.pinned_brief import (
                        deliver_pinned_startup_brief,
                        pinned_startup_brief_enabled,
                    )

                    if pinned_startup_brief_enabled():
                        try:
                            n_brief = await deliver_pinned_startup_brief(
                                broadcaster, client=client
                            )
                            LOG.info("watch_pinned_startup_brief", sent=n_brief)
                        except Exception:
                            LOG.exception("watch_pinned_startup_brief_failed")
                        _pinned_brief_sent = True
                ban_telemetry = client.rest_gate.guard.telemetry
                if ban_telemetry.last_at_mono and (
                    time.monotonic() - ban_telemetry.last_at_mono < interval_s + 5
                ):
                    LOG.warning(
                        "hunt_ccxt_ban_telemetry",
                        kind=ban_telemetry.last_kind,
                        context=ban_telemetry.last_context,
                        ip_bans=ban_telemetry.ip_ban_count,
                        rate_limits=ban_telemetry.rate_limit_count,
                        pause_remaining_s=round(client.rest_gate.guard.remaining_pause_s(), 1),
                        weight_used=client.rest_gate.weight_budget.used_weight,
                    )
                # P1.7: scheduled pump/dump digest (1h/3h/6h) — distinct from the
                # per-tick advisory batch. Candidates come from gated tickers.
                if send_telegram and broadcaster is not None:
                    digest_candidates = _build_digest_candidates(gated_ticker_rows)
                    sent_digest = await digest_scheduler.maybe_emit(
                        broadcaster, digest_candidates
                    )
                    if sent_digest:
                        LOG.info("hunt_digest_scheduled_sent", candidates=len(digest_candidates))
                # Periodic session checkpoint (~every 5 minutes)
                if time.monotonic() - _last_checkpoint >= 300.0:
                    try:
                        from hunt_core.runtime.state import save_session_checkpoint
                        cp = save_session_checkpoint(symbol_state)
                        if cp:
                            LOG.info("session_checkpoint_saved", path=str(cp.name))
                        _last_checkpoint = time.monotonic()
                    except Exception:
                        LOG.exception("session_checkpoint_save_failed")
                # Persist HTF frames (~every 5 min) so a hard-kill restart still
                # has a recent fallback — periodic because SIGKILL skips `finally`.
                if time.monotonic() - _last_htf_persist >= 300.0:
                    try:
                        from hunt_core.data.frame_cache import get_frame_cache as _gfc
                        from hunt_core.paths import HTF_FRAMES as _HTF_FRAMES

                        _n = _gfc().persist_htf_frames(_HTF_FRAMES)
                        _last_htf_persist = time.monotonic()
                        if _n:
                            LOG.debug("htf_frames_persisted", frames=_n)
                    except Exception:
                        LOG.exception("htf_frames_persist_failed")
                save_pump_history(pump_store)
                buffer_tick_rows(rows)
                if (
                    OUT_PATH.exists()
                    and OUT_PATH.stat().st_size >= TICK_ROTATE_MIN_BYTES
                    and time.monotonic() - last_tick_rotate >= TICK_ROTATE_INTERVAL_S
                ):
                    try:
                        rot_stats = rotate_hunt_ticks()
                        if rot_stats.get("appended_lines") or rot_stats.get("archived"):
                            LOG.info("hunt_tick_rotate_periodic", **rot_stats)
                        tel_stats = rotate_telemetry_jsonl()
                        if tel_stats.get("rotated"):
                            LOG.info("hunt_telemetry_rotate_periodic", **tel_stats)
                        last_tick_rotate = time.monotonic()
                    except Exception:
                        LOG.exception("hunt_tick_rotate_periodic_failed")
                if once:
                    print(serde.dumps_str(rows, indent=True))
                    break
            except Exception:
                LOG.exception("dump_watch_tick_error")
                _wd_beat()  # a handled tick error is progress — don't let the watchdog fire on recovery
                if once:
                    raise
            if once:
                break
            deadline = started + max(1.0, float(interval_s))
            while time.monotonic() < deadline and not should_stop():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(1.0, remaining))
    finally:
        # Capture the freshest HTF frames on graceful shutdown so the next start
        # reloads current data (periodic persist covers SIGKILL). Best effort.
        if not once:
            try:
                from hunt_core.data.frame_cache import get_frame_cache as _gfc
                from hunt_core.paths import HTF_FRAMES as _HTF_FRAMES

                _gfc().persist_htf_frames(_HTF_FRAMES)
            except Exception:
                LOG.exception("htf_frames_persist_shutdown_failed")
        if _wd_task is not None:
            _wd_task.cancel()
        faulthandler.cancel_dump_traceback_later()
        try:
            _wd_file.close()
        except Exception:
            LOG.exception("hunt_watchdog_close_failed")
        try:
            flush_lake()
        except Exception:
            LOG.exception("tick_buffer_flush_failed")
        try:
            from hunt_core.maps.engine import get_map_store
            from hunt_core.paths import MAPS_LAKE_JSONL

            get_map_store().flush_lake(MAPS_LAKE_JSONL)
        except Exception:
            LOG.exception("maps_lake_flush_failed")
        feature_lake.close()
        if tg_task is not None:
            tg_task.cancel()
            try:
                await tg_task
            except asyncio.CancelledError:
                pass
        if manipulation_task is not None:
            manipulation_task.cancel()
            try:
                await manipulation_task
            except asyncio.CancelledError:
                pass
        if deep_task is not None:
            deep_task.cancel()
            try:
                await deep_task
            except asyncio.CancelledError:
                pass
        if path_backfill_task is not None:
            path_backfill_task.cancel()
            try:
                await path_backfill_task
            except asyncio.CancelledError:
                pass
        if tg_cmds is not None:
            await tg_cmds.close()
        if market_runtime is not None:
            try:
                await market_runtime.close()
            except Exception:
                LOG.exception("engine_coexist_close_failed")
        try:
            await plane.aclose()
        except Exception:
            LOG.exception("hunt_plane_close_failed")


__all__ = ["run_loop"]
