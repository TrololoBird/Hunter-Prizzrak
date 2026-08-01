"""Watch main loop — universe, tick scheduling (Phase 8 split).

Воронка вселенной (`prescan`) снята вместе с модулем МАНИПУЛЯЦИИ 2026-07-31: она набирала
НЕпиннутую вселенную под сканер, тогда как призрак работает по пиннутым мажорам и `/signal SYM`.
Состав тика теперь — пиннутые ∪ CLI ∪ открытые позиции трекера.
"""
from __future__ import annotations

import asyncio
import faulthandler
import html
import os
import time
from collections.abc import Sequence
from typing import Any

from hunt_core.engine import metrics
from hunt_core import clock, serde
from hunt_core.view.runtime import MarketRuntime, build_market_runtime
from hunt_core.data.lake import FeatureLakeWriter, buffer_tick_rows, flush_lake
from hunt_core.data.universe import PINNED_SYMBOLS, resolve_watch_universe
from hunt_core.deliver.telegram import TelegramBroadcaster
from hunt_core.domain.config import (
    TICK_ROTATE_INTERVAL_S,
    TICK_ROTATE_MIN_BYTES,
)
from hunt_core.regime.market_regime import (
    REGIME_REFRESH_S,
    apply_snapshot,
    load_regime_file,
    refresh_market_regime,
)
from hunt_core.errors import DEFENSIVE_EXC, system_breakers
from hunt_core.maps.engine import get_map_store
from hunt_core.market.symbol_gate import gate_symbol_list
from hunt_core.market.symbols import fetch_ticker_rows
from hunt_core.params.store import migrate_calibration_split
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
from hunt_core.track.pump_history import (
    backfill_from_jsonl,
    load_pump_history,
    observe_prices,
    save_pump_history,
)
from hunt_core.track.tracker import iter_active_tracker_symbols, load_tracker_state
from hunt_core.domain.config import load_settings
from hunt_core.market.network import detect_local_proxies, ws_transport_fatal


_ORPHAN_WS_LOG_STATE: dict[str, float] = {"count": 0.0, "next_emit": 0.0}
_ORPHAN_WS_LOG_INTERVAL_S = 60.0
# Consecutive critical-blackout ticks before a supervised self-restart (auto-recovery
# for a stalled WS plane that the progress watchdog can't see). The alert fires at
# streak≥3, so this leaves ~7 ticks of warning first; never fires on an IP ban.
_BLACKOUT_RESTART_STREAK = int(os.getenv("HUNT_BLACKOUT_RESTART_STREAK", "10"))

# ── Троттлинг алерта блэкаута ──────────────────────────────────────────────────
# ⚠ ЗАМЕР 2026-07-27 по живому каналу: за 20 ч ушло **246 сообщений, из них 95 — этот алерт**
# (39% всего канала). Эпизодов при этом было **11**: два длинных (74 и 34 тика) дали ~104
# отправки, остальные девять — по одному-два тика. Алерт стоял без всякого троттлинга и уходил
# КАЖДЫЙ тик (30 с), пока держался стрик; дедуп broadcaster'а (sha256 текста) его не ловил,
# потому что в текст подставлен растущий счётчик тиков — каждое сообщение уникально побайтово.
#
# И ни одного сообщения о ВОССТАНОВЛЕНИИ: оператор получал 72 тревоги подряд и ни одной отбойной,
# то есть канал не отвечал на единственный вопрос, который у него есть, — «сейчас-то живо?».
_BLACKOUT_ALERT_COOLDOWN_S = float(os.getenv("HUNT_BLACKOUT_ALERT_COOLDOWN_S", "900") or 900)


def _blackout_numeral(n: int) -> str:
    """«3 тика» / «34 тика» / «11 тиков» — согласование числительного.

    Печаталось «3 тиков», «34 тиков». Мелочь, но это первое, что читает оператор в тревоге."""
    if 11 <= n % 100 <= 14:
        return "тиков"
    return {1: "тик", 2: "тика", 3: "тика", 4: "тика"}.get(n % 10, "тиков")


def _egress_hint() -> str:
    """Куда реально ходит бот за данными — из конфигурации, а не из памяти автора текста.

    Подсказка была захардкожена строкой «соединение прямое, без прокси». Сегодня она верна
    (`config.toml [bot.network]` без `proxy_url`), но верна СЛУЧАЙНО: она ничего не читает и
    станет ложью в тот день, когда появится `HTTPS_PROXY`/`BINANCE_PROXY_URL` — а это первое,
    что оператор пойдёт проверять по тревоге. ⚠ Не путать с прокси ТЕЛЕГРАМА
    (`detect_local_proxies`, лог `watch_telegram_proxy`): он к плоскости данных не относится."""
    from hunt_core.market.network import mask_proxy_url, resolve_proxy_url

    proxy = resolve_proxy_url()
    if proxy:
        return (
            f"Egress через прокси <code>{html.escape(mask_proxy_url(proxy))}</code> — "
            "проверьте, что он жив; либо зависший фетч. Сигналы не формируются."
        )
    return (
        "Egress прямой (прокси не настроен) — проверьте доступ к Binance "
        "или зависший фетч. Сигналы не формируются."
    )


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

    _TICK_LOCK = _loop_impl._TICK_LOCK

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _prev_loop_handler = asyncio.get_running_loop().get_exception_handler()

    def _hunt_loop_exc_handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        exc = context.get("exception")
        if exc is not None and ws_transport_fatal(exc):
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
        # ⚠ Здесь стояло `LOG.debug("hunt_calibration_rebuild_skipped",
        # reason="module_unavailable")` — НА ПУТИ УСПЕХА, сразу после удавшегося импорта и
        # выполненного вызова. Оба утверждения были ложны: ничего не пропущено, модуль
        # доступен (иначе строка бы не исполнилась). Это name-lie, фирменный класс дефектов
        # проекта, и печатался он при КАЖДОМ старте.
        #
        # Ложь в логе дороже молчания: молчание заставляет посмотреть, ложь — посмотреть не
        # туда. Разбор 2026-08-01 начался именно с попытки понять, какой модуль недоступен.
        LOG.debug("hunt_calibration_cache_invalidated")
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

    # ── ADR-0004: the engine-native MarketRuntime is the sole transport now. The legacy market plane
    # (HuntCcxtClient/HuntCcxtStreams/spot companion) is deleted; the ccxt.pro engine (MultiEngine +
    # cross-venue + SpotEngine) over the pinned+cli universe replaces it. Startup network/DNS can be
    # transiently down (host just woke from sleep); Binance is reached directly, so retry the whole
    # runtime build a few times with backoff before degrading. A failure leaves market_runtime=None
    # and the loop degrades to "engine unavailable" (logged, tasks skipped) — never a legacy fallback.
    from hunt_core.runtime.tick_state import set_live_market_runtime, set_live_spot_engine

    market_runtime: MarketRuntime | None = None
    eng_fut, eng_spot = _engine_universe(PINNED_SYMBOLS, cli_symbols)
    for _attempt in range(1, 4):
        try:
            market_runtime = build_market_runtime(eng_fut, spot_symbols=eng_spot)
            await market_runtime.start()
            break
        except Exception:
            LOG.exception("engine_runtime_start_retry", attempt=_attempt)
            market_runtime = None
            if _attempt < 3:
                await asyncio.sleep(20.0 * _attempt)
    if market_runtime is not None:
        set_live_spot_engine(market_runtime.spot)
        set_live_market_runtime(market_runtime)
        LOG.info("engine_runtime_started", futures=len(eng_fut), spot=len(eng_spot))
    else:
        set_live_spot_engine(None)
        set_live_market_runtime(None)
        LOG.error("engine_runtime_unavailable | watch loop degraded, no legacy fallback")
    # The engine's primary ccxt.pro client — the exchange handle every drained consumer takes
    # (regime refresh, path-backfill, the tick's universe funnel). None ⇒ degraded.
    exchange = market_runtime.multi.primary.exchange if market_runtime is not None else None
    # Persistent across ticks: prev-tick OI carry for oi_flush/oi_build.
    prev_oi: dict[str, float | None] = {}
    last_bias: dict[str, str] = {}
    last_lifecycle_phase: dict[str, str] = {}
    symbol_state = new_session_state()

    feature_lake = FeatureLakeWriter()

    pump_store = load_pump_history()
    if not pump_store.symbols and not pump_store.event_log:
        backfill_from_jsonl(pump_store)
        save_pump_history(pump_store)

    # --once smoke: skip the heavy first-tick regime refresh.
    _now_mono = time.monotonic()
    last_regime = _now_mono if once else 0.0
    # Cross-venue is now engine-native (MultiEngine secondaries), so the legacy REST cross-ex cache and
    # secondary-CEX ticker overlay are permanently empty here — kept only as the soft inputs the
    # (cross-ignoring) run_tick still accepts.
    _cross_ex_cache: dict[str, dict[str, Any]] = {}
    _secondary_ticker_overlay: dict[str, dict[str, Any]] = {}
    last_tick_rotate = time.monotonic()
    cached = load_regime_file()
    if cached is not None:
        apply_snapshot(cached)
    if not once and exchange is not None:
        try:
            await refresh_market_regime(exchange)
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
        try:
            await broadcaster.send_html(
                "🟢 <b>Hunt live</b>\n"
                f"Interval {interval_s}s · confirm-only alerts\n"
                "<i>Не auto-trade</i>"
            )
            startup_sentinel.parent.mkdir(parents=True, exist_ok=True)
            startup_sentinel.write_text(clock.now_utc().isoformat(), encoding="utf-8")
            LOG.info("watch_startup_telegram_sent", chat=settings.target_chat_id, cold_start=True)
        except Exception:
            LOG.exception("watch_startup_telegram_failed")

    # /signal polling conflicts with a second getUpdates consumer — only when TG sends enabled.
    tg_cmds = (
        build_hunt_telegram_commands(settings, proxy_url=proxy_url)
        if send_telegram and settings.tg_token
        else None
    )
    tg_task: asyncio.Task[None] | None = None
    if tg_cmds is not None:
        tg_task = asyncio.create_task(tg_cmds.run_forever(), name="hunt_tg_commands")
        LOG.info("hunt_telegram_commands_scheduled")

    deep_task: asyncio.Task[None] | None = None
    _deep_enabled = os.getenv("HUNT_DEEP_PINNED_LOOP", "1").strip().lower() not in {"0", "false", "no"}
    if not once and _deep_enabled and market_runtime is not None:
        # ADR-0004 Phase 9: the deep/analyst lane runs on the engine-native MarketRuntime (typed
        # MarketView per symbol), not the legacy client/ws_feed. No engine → the loop is skipped.
        from hunt_core.runtime.analyst_assembly import analyst_pinned_loop

        deep_task = asyncio.create_task(
            analyst_pinned_loop(market_runtime, broadcaster, send_telegram=send_telegram),
            name="analyst_pinned_loop",
        )
        LOG.info("analyst_pinned_loop_scheduled")
    elif not once and _deep_enabled and market_runtime is None:
        LOG.error("analyst_pinned_loop_disabled | engine runtime unavailable")

    path_backfill_task: asyncio.Task[None] | None = None
    if not once and exchange is not None:
        from hunt_core.track.path_backfill import path_backfill_loop

        path_backfill_task = asyncio.create_task(
            path_backfill_loop(exchange, interval_s=900.0),
            name="path_backfill_loop",
        )
        LOG.info("path_backfill_scheduled", interval_s=900.0)

    # Macro доп-факторы (dominance / market cap). The tick reads their caches SYNCHRONOUSLY and
    # cache-only, so without this producer the config flags were stubs — enabling one read an empty
    # cache and the factor silently no-opped. Self-disables when both flags are off.
    macro_refresh_task: asyncio.Task[None] | None = None
    if not once:
        from hunt_core.prizrak.macro_refresh import macro_context_refresh_loop

        macro_refresh_task = asyncio.create_task(
            macro_context_refresh_loop(tuple(PINNED_SYMBOLS)),
            name="macro_context_refresh",
        )

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
    # HTF frames live in the engine's kline planes now (seeded + WS-streamed by MultiEngine); the
    # legacy frame-cache persist/reload is gone, so a restart re-seeds off the engine, not a JSON blob.
    _degraded_streak = 0  # consecutive ticks the whole universe failed data assembly
    # Троттлинг алерта блэкаута: когда тревога ушла в последний раз (monotonic) и какой стрик
    # был максимальным за эпизод — второе печатается в отбойном сообщении.
    _blackout_alert_at: float | None = None
    _blackout_alert_peak = 0
    try:
        tick_ctx: dict[str, Any] | None = None
        while not should_stop():
            started = time.monotonic()
            try:
                if (
                    not once
                    and exchange is not None
                    and time.monotonic() - last_regime >= REGIME_REFRESH_S
                ):
                    try:
                        snap = await refresh_market_regime(exchange)
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

                settings = load_settings()
                now = clock.now_utc()
                # Whole-universe 24h tickers off the engine ccxt exchange (fail-loud []).
                # Cross-venue is engine-native now, so the legacy secondary-CEX overlay is
                # permanently empty (soft) and the per-symbol OI %-change cache is gone.
                ticker_raw = (
                    await asyncio.wait_for(fetch_ticker_rows(exchange), timeout=120.0)
                    if exchange is not None
                    else []
                )
                ticker_by_sym = {str(t.get("symbol")): t for t in ticker_raw if t.get("symbol")}
                ex = exchange
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
                merged = (
                    gate_symbol_list(merged, exchange=ex, label="watch_universe")
                    if ex is not None
                    else merged
                )
                active = tuple(dict.fromkeys(merged))
                active = tuple(s for s in active if not is_blacklisted(s))
                # No demand-shaping load planner and no lake-warmup pre-fetch: the engine's ccxt
                # throttler owns REST weight, the MarketView is read-through per symbol, and the engine
                # seeds/streams the kline planes. A symbol outside the engine warm-set simply reports
                # not-ready this tick (dynamic warm-set add is a later phase), never client-warmed.
                hunt_active = tuple(active)
                LOG.info("watch_universe", symbols=len(hunt_active), list=list(active)[:8])

                # ADR-0004 Phase 9: the main tick runs on the engine-native MarketRuntime (typed
                # MarketView per symbol) + the map store, not the legacy client/ws_feed/batch cache.
                tick_ctx = {
                    "active": active,
                    "settings": settings,
                    "rt": market_runtime,
                    "store": get_map_store(),
                    "prev_oi": prev_oi,
                    "last_bias": last_bias,
                    "last_lifecycle_phase": last_lifecycle_phase,
                    "mode_map": mode_map,
                    "broadcaster": broadcaster,
                    "send_telegram": send_telegram,
                    "ticker_by_sym": ticker_by_sym,
                    "pump_store": pump_store,
                    "cross_ex_cache": _cross_ex_cache,
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

                # The main tick is engine-native now — no engine runtime means no MarketView, so the
                # tick is skipped (logged), never client-fed. The deep/scanner lanes degrade the same.
                if market_runtime is None:
                    LOG.error("watch_tick_disabled | engine runtime unavailable")
                    rows = []
                else:
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
                        # Пик копится КАЖДЫЙ тик, а не в момент отправки: тревоги уходят раз в
                        # 15 мин (стрики 3, 33, 63…), и отбой по «последнему отправленному»
                        # напечатал бы для эпизода в 74 тика цифру 63 — занижение на 15%.
                        _blackout_alert_peak = max(_blackout_alert_peak, _degraded_streak)
                        LOG.warning(
                            "hunt_universe_degraded",
                            streak=_degraded_streak,
                            **_health.telemetry(),
                        )
                        # IP-ban detection used to read the legacy REST gate's guard telemetry;
                        # the engine's ccxt.pro client owns rate-limit handling internally now, so a
                        # ban is no longer observable from here. Treat the blackout as non-ban (the
                        # conservative branch — the self-restart guard may then respawn on a sustained
                        # critical stall, which is the intended WS-mux recovery).
                        _ban_pause = 0.0
                        _is_ban = False
                        # Escalate to an ops alert once the blackout persists (not a
                        # one-off blip) — near-total failure across several ticks.
                        #
                        # Отправка — ОДИН РАЗ на эпизод, плюс повтор не чаще
                        # `_BLACKOUT_ALERT_COOLDOWN_S`, если блэкаут всё ещё держится. Прежде
                        # алерт уходил каждый тик и один эпизод давал до 72 одинаковых сообщений
                        # (замер выше). Повтор оставлен намеренно: молчащий час — это тоже
                        # неверный сигнал, оператор должен знать, что авария длится.
                        _now_mono = time.monotonic()
                        if (
                            _health.critical
                            and _degraded_streak >= 3
                            and send_telegram
                            and broadcaster is not None
                            and (
                                _blackout_alert_at is None
                                or (_now_mono - _blackout_alert_at) >= _BLACKOUT_ALERT_COOLDOWN_S
                            )
                        ):
                            # Cause-aware guidance: a Binance IP rate-limit ban (418/429) pauses
                            # the REST plane and starves the 4h refresh — it self-heals when the
                            # ban lifts, so say so instead of sending the operator hunting.
                            if _is_ban:
                                _cause_hint = (
                                    f"⏳ Binance IP-бан/rate-limit — REST на паузе (~{_ban_pause:.0f}s). "
                                    "Частота запросов уже снижена; ждём снятия, процесс восстановится сам."
                                )
                            else:
                                _cause_hint = _egress_hint()
                            # «7/7 символов (100%)» читалось как «вся вселенная погасла», хотя
                            # оценивается ТОЛЬКО прогретый набор тика (7 при `watch_universe` в
                            # 24–29 символов). Масштаб называем явно.
                            _repeat = "" if _blackout_alert_at is None else " (авария продолжается)"
                            try:
                                await broadcaster.send_html(
                                    f"🚨 <b>Data blackout</b>{_repeat}: "
                                    f"{_health.failures}/{_health.total} прогретых символов "
                                    f"({_health.failure_frac * 100:.0f}%) не проходят проверку "
                                    f"данных {_degraded_streak} "
                                    f"{_blackout_numeral(_degraded_streak)} подряд.\n"
                                    f"Причина: <code>{html.escape(str(_health.dominant_kind))}</code>.\n"
                                    f"{_cause_hint}"
                                )
                                _blackout_alert_at = _now_mono
                            except Exception:
                                LOG.exception("hunt_universe_degraded_alert_failed")
                        # AUTO-RECOVERY: a sustained critical NON-ban blackout (e.g. a
                        # stalled WS mux — 2026-07-13) doesn't trip the progress watchdog,
                        # so recover by exiting for a clean supervised respawn. The engine
                        # re-seeds its kline planes on restart, so there is no warmup blackout
                        # to pre-persist (the legacy HTF frame-cache dump is gone).
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
                            os._exit(1)
                    else:
                        # ОТБОЙ. Раньше эпизод просто переставал шуметь, и канал никогда не
                        # отвечал на единственный вопрос оператора — «сейчас-то живо?». Шлётся
                        # только тем, кого тревожили: без предшествующего алерта ничего не идёт.
                        if _blackout_alert_at is not None:
                            _peak = max(_blackout_alert_peak, 1)
                            if send_telegram and broadcaster is not None:
                                try:
                                    await broadcaster.send_html(
                                        "✅ <b>Data blackout снят</b>: данные снова проходят "
                                        f"проверку ({_health.total - _health.failures}/"
                                        f"{_health.total} прогретых символов).\n"
                                        f"Пик аварии — {_peak} {_blackout_numeral(_peak)} подряд."
                                    )
                                except Exception:
                                    LOG.exception("hunt_universe_recovered_alert_failed")
                            LOG.info("hunt_universe_recovered", peak_streak=_peak)
                        _blackout_alert_at = None
                        _blackout_alert_peak = 0
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
                                broadcaster, rt=market_runtime
                            )
                            LOG.info("watch_pinned_startup_brief", sent=n_brief)
                        except Exception:
                            LOG.exception("watch_pinned_startup_brief_failed")
                        _pinned_brief_sent = True
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
            # ⚠ `--interval` — НИЖНЯЯ ГРАНИЦА, А НЕ ПЕРИОД, и до 2026-08-01 это молчало.
            #
            # Когда тело тика длиннее интервала, внутренний `while` не выполняется ни разу:
            # цикл уходит на следующую итерацию без сна. Ни строки лога, ни метрики — то
            # есть система переходила в свободный бег незаметно, а оператор крутил ручку,
            # которая не связывает.
            #
            # ЗАМЕР аудита по персистнутым строкам тика (`data/hunt_scan-*.jsonl`, 1232
            # межтиковых интервала): медиана периода **34.0 с** при `--interval 30`,
            # p90 158.7 с, p99 1992 с, max 2817 с — **69.2% интервалов за бортом**.
            # Рядом в конфиге `SYMBOL_TICK_TIMEOUT_S = 180` при интервале 30: один
            # залипший символ легально растягивает тик в шесть раз.
            #
            # Теперь просрочка объявляется, а фактическая длительность тика публикуется
            # метрикой. Это не «настроить каденс», а сделать его измеримым: настраивать
            # окно без замера запрещает I-7, и первым шагом обязан быть замер.
            tick_s = time.monotonic() - started
            metrics.set_tick_duration(tick_s)
            deadline = started + max(1.0, float(interval_s))
            if tick_s > float(interval_s):
                LOG.warning(
                    "watch_interval_overrun",
                    tick_s=round(tick_s, 1),
                    interval_s=float(interval_s),
                    overrun_s=round(tick_s - float(interval_s), 1),
                    note="тик длиннее интервала — период задаёт ТИК, а не настройка; "
                         "цикл идёт на следующую итерацию без сна",
                )
            while time.monotonic() < deadline and not should_stop():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(1.0, remaining))
    finally:
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
        if macro_refresh_task is not None:
            macro_refresh_task.cancel()
            try:
                await macro_refresh_task
            except asyncio.CancelledError:
                pass
        if tg_cmds is not None:
            await tg_cmds.close()
        if market_runtime is not None:
            try:
                await market_runtime.close()
            except Exception:
                LOG.exception("engine_runtime_close_failed")


__all__ = ["run_loop"]
