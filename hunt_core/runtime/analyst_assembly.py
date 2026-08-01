"""Module 1 Deep tick orchestrator — pinned continuous + on-demand query plane (typed native).

ADR-0004 Phase 9: the deep lane consumes the typed :class:`NativeAnalystView` end-to-end. There is
no row dict here any more — ``assemble_native_analyst`` composes the view/features/maps/prizrak +
side-channels, and every function below reads those typed handles. The on-disk deep-tick JSONL is a
calibration/diagnostics serializer (allowed disk format), not a transport for a legacy row.
"""
from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Any

import structlog

from hunt_core import clock, serde
from hunt_core.data.universe import PINNED_SYMBOLS
from hunt_core.paths import ANALYST_TICKS_JSONL
from hunt_core.prizrak.engines.config import load_analyst_config
from hunt_core.prizrak.engines.delivery_policy import pick_hero_row
from hunt_core.prizrak.engines.signal_queue import load_signal_queue
from hunt_core.runtime.emitter import SignalEmitter

if TYPE_CHECKING:
    from hunt_core.maps.engine import MapTimeSeriesStore
    from hunt_core.runtime.native_assembly import NativeAnalystView
    from hunt_core.view.runtime import MarketRuntime

LOG = structlog.get_logger("hunt.analyst_assembly")


def _pinned_cfg(key: str) -> Any:
    """Значение из секции ``[analyst.pinned]`` файла настроек. ``None`` — ключа нет.

    ⚠ СЕКЦИЯ БЫЛА ФАНТОМНОЙ ЦЕЛИКОМ. `config.defaults.toml` объявляет `[analyst.pinned]`
    с `interval_s`, `tg_on_change` и `tg_stale_hours`, а код читал ТОЛЬКО переменные
    окружения. Свип 2026-08-01: у `tg_on_change` и `tg_stale_hours` ноль читателей во всём
    дереве, `interval_s` читался как `HUNT_DEEP_PINNED_INTERVAL`. То есть правка TOML была
    МОЛЧАЛИВЫМ no-op — ровно ловушка, о которой предупреждает CLAUDE.md («editing the TOML
    silently no-ops»), и владелец, крутивший интервал в конфиге, не менял ничего.

    Порядок теперь: TOML задаёт значение, переменная окружения ПЕРЕКРЫВАЕТ его (аварийный
    рычаг оператора). Не наоборот: `config.defaults.toml` объявлен истиной, и он ей должен
    быть.

    ⚠ ЧЕРЕЗ ``config_section``, А НЕ ``load_config_defaults_toml``. Вторая — форвардер по
    белому списку и отдаёт только ``{"gates", "tracker"}``; секции ``analyst`` в её выводе
    НЕТ. Первая редакция этой правки читала именно её, и TOML продолжал игнорироваться —
    поймал собственный контроль (подменил значение на 777, код видел 300), а не обзор кода.
    """
    try:
        from hunt_core.domain.config import config_section

        section = config_section("analyst", "pinned")
    except Exception as exc:  # noqa: BLE001 — отсутствие конфига не должно ронять полосу
        LOG.warning("analyst_pinned_cfg_unreadable", key=key, error=repr(exc))
        return None
    return section.get(key)


def analyst_pinned_interval_s() -> float:
    env = os.getenv("HUNT_DEEP_PINNED_INTERVAL")
    if env:
        return float(env)
    from_toml = _pinned_cfg("interval_s")
    if from_toml is not None:
        try:
            return float(from_toml)
        except (TypeError, ValueError):
            LOG.warning("analyst_pinned_interval_unparsable", value=repr(from_toml)[:40])
    return 300.0


def deep_tg_on_change() -> bool:
    env = os.getenv("HUNT_DEEP_TG_ON_CHANGE")
    if env is not None:
        return env.strip().lower() not in {"0", "false", "no"}
    from_toml = _pinned_cfg("tg_on_change")
    return True if from_toml is None else bool(from_toml)


# ── Выравнивание пробуждения по закрытию бара ─────────────────────────────────────────────
# Полоса эмиссии шла по СВОБОДНОМУ таймеру, то есть попадала в сетку закрытий баров случайной
# фазой. Лаг «бар закрылся → полоса его увидела» распределён тогда равномерно на [0, период],
# и медиана равна половине периода — это свойство конструкции, а не выборки.
#
# ЗАМЕР 2026-08-02 (`scripts/measure_bar_close_lag.py`, простои процесса исключены, n=78):
# лаг до 15m-бара med **160.1 с**, p90 **437.4 с**, max 582.8 с — то есть **17.8% бара в
# медиане и 48.6% в p90** проходило, прежде чем сигнал по нему вообще МОГ быть выпущен.
# Для сравнения главный тик (такт 30 с) на тех же данных: med 20.5 с, 2.3% бара.
#
# ⚠ ПОЧЕМУ СЕТКА ИМЕННО 5m. Период полосы (300 с) УЖЕ равен 5-минутному бару, а границы всех
# ТФ, которые читают тиры призрака (5m/15m/1h/4h/1d/1w — `prizrak/config.py`), КРАТНЫ пяти
# минутам. Значит фазовая привязка к 5m-сетке ставит закрытие ЛЮБОГО из них в пределах запаса
# от пробуждения, и при этом не меняет частоту обходов — меняется только фаза.
_BAR_GRID_S = 300.0

# Запас после границы: раньше него бар в движке ещё не закрыт, и полоса прочитала бы ПРОШЛЫЙ
# бар со свежим штампом — замороженный кадр, который не видит ни один прибор (сигнатура
# `stale-htf-cache-trap`). Проснуться рано здесь строго хуже, чем поздно.
#
# ЗАМЕР 2026-08-02 (`scripts/measure_bar_availability.py`, живой WS+REST, n=30, опрос 0.25 с):
# «граница бара → бар виден закрытым в движке» med **4.18 с**, p90 7.32 с, **max 10.60 с**.
# Путь появления (`engine/ingest.py::_step_ohlcv`): WS-сигнал о закрытии → REST full-fidelity
# (WS-бар без taker-объёма не мержится) → merge. От таймфрейма он не зависит, поэтому мерилось
# на 1m: тот же код, 15 замеров за время одного замера на 15m.
_BAR_SETTLE_S = 12.0  # max наблюдённый 10.60 + запас


def _next_bar_wake_ts(now: float, *, grid_s: float = _BAR_GRID_S, settle_s: float = _BAR_SETTLE_S) -> float:
    """Ближайшее «граница бара + запас» СТРОГО в будущем относительно ``now`` (epoch-секунды).

    Args:
        now: Текущее epoch-время (``time.time()``, не monotonic — сетка баров привязана к UTC).
        grid_s: Шаг сетки закрытий, секунды.
        settle_s: Запас на доставку бара движком после границы.

    Returns:
        Epoch-время следующего пробуждения по бару. Всегда строго больше ``now`` и никогда
        не дальше, чем на ``grid_s``.
    """
    # ⚠ СЧИТАТЬ НАДО ОТ ПРЕДЫДУЩЕЙ ГРАНИЦЫ, А НЕ ОТ СЛЕДУЮЩЕЙ. Первая редакция брала границу
    # СТРОГО ПОСЛЕ `now` — и на входе `now` = ровно граница бара возвращала пробуждение через
    # **312 с вместо 12 с**: только что закрывшийся бар пропускался целиком, а полоса ждала
    # следующего. Худший из возможных промахов, потому что обход как раз и заканчивается
    # около границы. Ветка «если запас уже прошёл, взять следующую» при этом была МЁРТВОЙ
    # (`boundary > now` выполнялось всегда) и ровно этим дефект и маскировала.
    # Поймано случайной проверкой инварианта (832 нарушения из 20000), а не чтением кода.
    prev_boundary = (now // grid_s) * grid_s
    if prev_boundary + settle_s > now:
        return prev_boundary + settle_s
    return prev_boundary + grid_s + settle_s


def _compact_symbol(symbol: str) -> str:
    """Unified ``BTC/USDT:USDT`` → compact ``BTCUSDT`` for display/logging/cooldown keys."""
    return symbol.split(":", 1)[0].replace("/", "").upper()


def _serialize_native_tick(native: NativeAnalystView) -> dict[str, Any]:
    """Project the typed view onto the minimal deep-tick JSONL dict (disk format, not a bridge).

    Emits exactly the keys ``calibration.load_deep_tick_summaries`` reads back off disk
    (``symbol``/``ts``/``prizrak_summary``/``prizrak_abstain``) plus ``price`` for context. NOT a
    legacy-row reconstruction: no market/timeframes/lifecycle/mtf keys — those had no consumer here.
    """
    return {
        "symbol": _compact_symbol(native.view.symbol),
        "ts": native.freshness.get("as_of"),
        "price": native.view.last_price,
        "plane": "deep",
        "tick_path": "analyst_assembly",
        "prizrak_summary": native.prizrak.summary,
        "prizrak_abstain": list(native.prizrak.abstain),
    }


def append_deep_tick_jsonl(native: NativeAnalystView) -> None:
    """Append one deep tick to the calibration/diagnostics JSONL (serialized from typed handles)."""
    from hunt_core.data.jsonl_io import append_jsonl_lines

    ANALYST_TICKS_JSONL.parent.mkdir(parents=True, exist_ok=True)
    append_jsonl_lines(ANALYST_TICKS_JSONL, [serde.dumps_str(_serialize_native_tick(native))])


def material_deep_change(
    symbol: str,
    cur: NativeAnalystView,
    *,
    prev: NativeAnalystView | None,
) -> bool:
    """True when verdict action/path changed — telemetry only (TG uses lifecycle spine)."""
    _ = symbol
    if prev is None:
        return True
    _p = prev.prizrak.summary
    prev_summary = _p if isinstance(_p, dict) else {}
    _s = cur.prizrak.summary
    summary = _s if isinstance(_s, dict) else {}
    if str(prev_summary.get("action") or "wait") != str(summary.get("action") or "wait"):
        return True
    return str(prev_summary.get("path") or "") != str(summary.get("path") or "")


async def assemble_analyst_tick(
    symbol: str,
    rt: MarketRuntime,
    *,
    store: MapTimeSeriesStore,
) -> NativeAnalystView | None:
    """Full deep snapshot for one pinned/tracked symbol — typed native, fail-loud.

    Composes the :class:`NativeAnalystView` off the engine runtime (``assemble_native_analyst``),
    persists it to the in-memory deep store + the calibration JSONL, merges the live calibration
    sample, and refreshes the pinned signal queue. Returns ``None`` when the symbol has no live
    view (not in the engine warm-set, or no price) — never a fabricated view.
    """
    from hunt_core.runtime.native_assembly import assemble_native_analyst

    sym = str(symbol or "").upper()
    native = await assemble_native_analyst(rt, sym, store=store)
    if native is None:
        return None

    from hunt_core.runtime.tick_state import deep_query_store

    deep_query_store().put(sym, native)
    append_deep_tick_jsonl(native)

    summary = native.prizrak.summary
    if isinstance(summary, dict):
        try:
            from hunt_core.prizrak.engines.calibration import (
                CALIBRATION_JSON,
                merge_live_sample,
                write_calibration_rollup,
            )

            if CALIBRATION_JSON.is_file():
                report = serde.loads(CALIBRATION_JSON.read_text(encoding="utf-8"))
                report = merge_live_sample(report, summary, sym)
                CALIBRATION_JSON.write_text(serde.dumps_str(report, indent=True), encoding="utf-8")
            else:
                write_calibration_rollup(limit=200)
        except Exception as exc:
            LOG.debug("prizrak_calibration_skip", symbol=sym, error=repr(exc))

    try:
        from hunt_core.prizrak.engines.signal_queue import refresh_pinned_signal_queue

        v2cfg = load_analyst_config()
        if getattr(v2cfg, "signal_queue_enabled", True):
            refresh_pinned_signal_queue(sym, native, top_n=v2cfg.signal_queue_top_n)
    except Exception as exc:
        LOG.debug("prizrak_signal_queue_skip", symbol=sym, error=repr(exc))

    LOG.info(
        "prizrak_enrich_done",
        symbol=sym,
        summary_action=(summary or {}).get("action") if isinstance(summary, dict) else None,
        candidates=len(native.prizrak.signals),
    )
    return native


async def send_analyst_change_telegram(
    broadcaster: Any,
    native: NativeAnalystView,
    *,
    cycle_peers: list[NativeAnalystView] | None = None,
    lifecycle_event: str = "signal",
) -> int | None:
    """Send the deep-analysis Telegram card. Returns its ``message_id``, ``None`` if not sent.

    ⚠ ВОЗВРАЩАЕТ ID, А НЕ `bool`, и это не косметика. `emitter._register_tracker` заводит по
    этой карточке РЕАЛЬНУЮ отслеживаемую сделку, а `_cycle_reconcile._deliver_followup` шлёт её
    SL/TP/закрытие только когда у записи стоит `telegram_sent`/`entry_message_id` («канал про эту
    сделку слышал»). Ставить их было НЕЧЕМ: id уходил в лог и терялся. Замер 2026-07-27 —
    7 записей трекера без флага, и все семь пиннед-символы призрака; ETH-шорт получил в канал
    ЧЕТЫРЕ «✅ Активация», закрылся в 15:41:50 (`watch_auto_resolve`), и ни трейл, ни
    предупреждение о стопе, ни закрытие в канал не ушли — молча, без единой строки в логе.


    Два режима. **LONG/SHORT** — эмитированный сетап, проходит арбитра (кулдаун, качество).
    **WAIT** — карта уровней и причина отказа; это ОСНОВНОЙ жанр автора, а не отсутствие
    контента: разбор BTC 1ч от 2026-07-25 — 17 минут разметки и трёх причин, почему сделки нет.
    Раньше здесь стоял ранний ``return False`` на всём, что не long/short, и карточку отказа
    можно было увидеть только вручную через /signal. Гейт WAIT — смена ОТПЕЧАТКА карты зон
    (``arbiter.wait_card_fingerprint``), потому что по времени она бы шла каждый тик.
    """
    import html

    from hunt_core.prizrak.arbiter import (
        evaluate_deep_delivery,
        # `wait_card_fingerprint` здесь БОЛЬШЕ НЕ ИМПОРТИРУЕТСЯ, и это главный признак того, что
        # подпорка снята: решение об отправке больше не выводится из отпечатка карты. Сама функция
        # пока жива в `arbiter` — у неё может быть другой читатель; проверить и удалить отдельно.
        mark_wait_sent,
        wait_card_ok,
    )

    sym = _compact_symbol(native.view.symbol)
    _summ = native.prizrak.summary
    summary = _summ if isinstance(_summ, dict) else {}
    action = str(summary.get("action") or "wait").lower()
    wait_fp = ""
    if action not in {"long", "short"}:
        # Отпечаток берётся из РЕЕСТРА (состав зон + их статусы), а не из сырой карты: карта дрожит,
        # и прежний отпечаток по координатам разрешал новую отправку примерно раз в час на символ —
        # 142 карточки `wait` за прогон 2026-07-28 при нуле сделок. Реестровый меняется только когда
        # зона появилась или сменила статус, то есть ровно тогда, когда читателю есть что узнать.
        # Падать сюда нельзя: без отпечатка карточка не отправится вовсе, поэтому сырой остаётся
        # запасным вариантом, а не заменяется молча.
        # ⚠ РЕШЕНИЕ «есть ли что сказать» принимает РЕЕСТР, а не сравнение отпечатков.
        #
        # Прежняя схема строила карточку безусловно и отбрасывала её на выходе, сравнивая отпечаток
        # карты с прошлым. Замер 2026-07-28: **64 подавления на 11 отправок**, то есть 85% работы в
        # корзину. Но дороже расхода была ДУБЛИРОВАННАЯ ЛОГИКА: реестр уже знает, что зона сменила
        # статус (он сам её и переводит), а арбитр выводил это заново из строк сообщения. Копия
        # знания разъезжалась с оригиналом дважды за день — на неустойчивости корзины якоря и на
        # чувствительности к дрожанию полос, — и оба раза давала ложные отправки.
        #
        # Теперь повод — накопленные переходы (`created` / `confirmed` / `worked` / `broken`).
        # Очередь, а не флаг: между двумя карточками умещается ~10 тиков реестра.
        _setups = getattr(native.prizrak, "setups", None)
        events: list[dict[str, Any]] = []
        try:
            from hunt_core.prizrak.zone_registry import peek_events
            from hunt_core.track.tracker import load_tracker_state

            events = peek_events(load_tracker_state(), sym)
        except Exception:  # noqa: BLE001 — реестр недоступен: падаем на прежний отпечаток
            events = []
        if events:
            # Отпечаток по составу событий: он меняется ровно тогда, когда пришёл НОВЫЙ переход,
            # и не двигается от дрожания карты. Прежний `mark_wait_sent` продолжает работать как
            # был — меняется только то, ЧТО считается изменением.
            wait_fp = "|".join(f"{e.get('zone_id')}:{e.get('event')}" for e in events)
        else:
            # Событий нет — сказать нечего. Ранний возврат вместо «построить и подавить»: это и
            # есть снятие подпорки, ради которого схема переделана.
            LOG.info("analyst_pinned_tg_skipped_wait", symbol=sym, action=action,
                     reason="no_zone_events")
            return None
        if not wait_card_ok(sym, wait_fp):
            LOG.info("analyst_pinned_tg_skipped_wait", symbol=sym, action=action,
                     reason="map_unchanged" if wait_fp else "no_zones")
            return None
    else:
        ok, blockers = evaluate_deep_delivery(symbol=sym, verdict=summary)
        if not ok:
            LOG.info("analyst_pinned_tg_skipped_arbiter", symbol=sym, blockers=blockers)
            return None

    blocks: list[str] = []
    if lifecycle_event == "activated":
        sym_label = sym.replace("USDT", "-USDT")
        # rr_primary is None whenever geometry is incomplete (orchestrator.py:653), so an
        # unguarded f-string put a literal «R:R (от входа) None» at the top of the most
        # action-inducing message. Drop the clause instead.
        rr = summary.get("rr_primary")
        head = f"✅ <b>Активация</b> · {html.escape(sym_label)}"
        if isinstance(rr, (int, float)):
            head += f" · R:R (от входа) <code>{float(rr):.2f}</code>"
        blocks.append(head)

    from hunt_core.prizrak.build import build_deep_report
    from hunt_core.prizrak.format_telegram import format_deep_analysis_telegram

    analysis = build_deep_report(native, include_watch_appendix=False)
    blocks.append(format_deep_analysis_telegram(analysis))

    # ТОЛЬКО карточка. Грид конфлюенса, карты внутридневной ликвидности, соседи по циклу и очередь
    # сигналов приклеивались к КАЖДОМУ сообщению и вместе с ней давали 3883 символа при лимите
    # 3900 — карточка физически не влезала и уходила двумя кусками (замерено на живом прогоне
    # 2026-07-26: 21 отправка на 11 карточек). Причём очередь показывала ЧУЖОЙ символ (в карточке
    # BTC — «#1 ETH-USDT ЛОНГ»), а её заголовок печатал подстановки «ранг # (позиция)» как текст.
    # Автор публикует пост про ОДИН инструмент; всё остальное — это отдельные продукты, и место им
    # в своих сообщениях, а не в хвосте чужого.
    from hunt_core.runtime.query_service import format_row_freshness_footer
    # As-of stamp, last line. The broadcaster buffers on circuit-open and replays later, so a
    # pinned card can land long after it was built — without this the reader can't tell.
    blocks.append(format_row_freshness_footer(native, source="analyst tick"))
    result = await broadcaster.send_html("\n".join(blocks))
    if result.status == "sent":
        if wait_fp:
            mark_wait_sent(sym, wait_fp)
        # Очередь переходов чистится ТОЛЬКО после подтверждённой отправки. Слить её раньше —
        # например, при построении карточки — значит потерять повод, если Telegram ответит
        # ошибкой: следующий цикл не увидит событий и промолчит о том, что уже произошло.
        # Именно поэтому выше стоит `peek_events`, а не `drain_events`.
        if events:
            try:
                from hunt_core.prizrak.zone_registry import drain_events
                from hunt_core.track.tracker import load_tracker_state, save_tracker_state

                _st = load_tracker_state()
                drain_events(_st, sym)
                save_tracker_state(_st)
            except Exception:  # noqa: BLE001 — очистка очереди не должна ронять доставку
                LOG.exception("zone_events_drain_failed", symbol=sym)
        LOG.info("analyst_pinned_tg_sent", symbol=sym, message_id=result.message_id,
                 plane="deep", kind="wait" if wait_fp else action,
                 events=[e.get("event") for e in events] or None)
        return result.message_id
    LOG.warning("analyst_pinned_tg_failed", symbol=sym, status=result.status, reason=result.reason)
    return None


def _prizrak_row_variants(native: NativeAnalystView) -> list[tuple[NativeAnalystView, str]]:
    """Expand a pinned view into one lifecycle variant per Prizrak setup_kind.

    Prizrak produces 0..N independent candidates per tick (``prizrak.signals``); each setup_kind
    (level_core / pp_break / trap_flip / level_intraday_scalp / zone_target_deep …) is a distinct
    thesis and should get its own Telegram message — the lifecycle spine dedups by setup_id so
    re-runs don't spam. Each variant swaps ``prizrak.summary`` to the strongest candidate of that
    setup_kind (``PrizrakOutput`` is frozen → ``model_copy``; the view is a NamedTuple → ``_replace``).
    Falls back to the view as-is when there is ≤1 candidate (single-summary behaviour preserved).
    """
    sigs = native.prizrak.signals
    if len(sigs) <= 1:
        _summ = native.prizrak.summary
        summary = _summ if isinstance(_summ, dict) else {}
        return [(native, str(summary.get("setup_kind") or "deep"))]
    best_by_kind: dict[str, dict[str, Any]] = {}
    for c in sigs:
        if not isinstance(c, dict):
            continue
        kind = str(c.get("setup_kind") or "deep")
        cur = best_by_kind.get(kind)
        if cur is None or float(c.get("strength") or 0) > float(cur.get("strength") or 0):
            best_by_kind[kind] = c
    variants: list[tuple[NativeAnalystView, str]] = []
    for kind, cand in best_by_kind.items():
        variant = native._replace(prizrak=native.prizrak.model_copy(update={"summary": cand}))
        variants.append((variant, kind))
    return variants


async def analyst_pinned_loop(
    rt: MarketRuntime | None,
    broadcaster: Any | None,
    *,
    interval_s: float | None = None,
    send_telegram: bool = True,
    store: MapTimeSeriesStore | None = None,
) -> None:
    """Background continuous deep analysis for pinned anchors (engine-native transport)."""
    import asyncio

    from hunt_core.maps.engine import get_map_store
    from hunt_core.runtime.state import should_stop
    from hunt_core.runtime.tick_state import live_market_runtime

    rt = rt or live_market_runtime()
    if rt is None:
        LOG.error("analyst_pinned_loop_disabled | engine runtime unavailable")
        return
    store = store or get_map_store()

    interval = interval_s if interval_s is not None else analyst_pinned_interval_s()
    LOG.info("analyst_pinned_loop_start", symbols=list(PINNED_SYMBOLS), interval_s=interval)
    # ⚠ Дедлайн берётся ДО обхода, а не после. Смотри пояснение у сна в конце цикла.
    walk_started = 0.0
    while not should_stop():
        walk_started = time.monotonic()
        v2cfg = load_analyst_config()
        emitter = SignalEmitter()
        lifecycle_candidates: list[tuple[NativeAnalystView, Any, str]] = []
        wait_candidates: list[NativeAnalystView] = []
        for sym in PINNED_SYMBOLS:
            if should_stop():
                break
            try:
                native = await assemble_analyst_tick(sym, rt, store=store)
                if native is None:
                    LOG.info("analyst_pinned_tick_not_ready", symbol=sym)
                    continue
                # Lifecycle spine is the SOLE emission gate — dedup/cooldown/silence all live in
                # process_lifecycle_tick. A7: one lifecycle candidate per Prizrak setup_kind.
                variants = _prizrak_row_variants(native)
                for variant, kind in variants:
                    transition = emitter.preview_deep_row(variant)
                    if transition.event != "none":
                        lifecycle_candidates.append((variant, transition, kind))
                # Символ без эмиссии — не «нечего сказать»: это карта уровней плюс причина отказа,
                # основной жанр автора. Гейт (смена отпечатка карты) живёт в
                # send_analyst_change_telegram, поэтому кандидат добавляется безусловно.
                _s = native.prizrak.summary
                if str((_s or {}).get("action") or "wait").lower() not in {"long", "short"}:
                    wait_candidates.append(native)
            except Exception:
                LOG.exception("analyst_pinned_loop_symbol_failed", symbol=sym)

        if send_telegram and broadcaster is not None and lifecycle_candidates:
            from hunt_core.prizrak.arbiter import (
                deep_cooldown_ok,
                mark_deep_sent,
                observe_outcome_gates,
            )

            queue = load_signal_queue()
            # Кулдаун применяется ДО выбора героя. Раньше схлопывание шло первым, а кулдаун
            # проверялся уже внутри цикла отправки — если герой оказывался остывшим, цикл не слал
            # НИЧЕГО, хотя другой кандидат на другом символе был готов. `pick_hero_row` ранжирует
            # по силе и про кулдаун не знает, поэтому сильный символ мог голодом морить чужой
            # сетап по 300 с за раз, а лог тихого цикла был неотличим от «ничего не сработало».
            eligible = [
                (n, tr, k)
                for n, tr, k in lifecycle_candidates
                if deep_cooldown_ok(f"{_compact_symbol(n.view.symbol)}:{k}")
            ]
            if not eligible:
                LOG.info(
                    "deep_all_candidates_in_cooldown",
                    candidates=len(lifecycle_candidates),
                    symbols=[_compact_symbol(n.view.symbol) for n, _, _ in lifecycle_candidates][:5],
                )
            natives_only = [n for n, _, _ in eligible]
            if v2cfg.signal_queue_tg_batch and len(eligible) > 1:
                # Batch mode: collapse to a single hero message (config-controlled). Multi-emission
                # (one message per setup_kind) is the non-batch path below.
                hero = pick_hero_row(natives_only, queue)
                to_send = (
                    [(hero, tr, k) for n, tr, k in eligible if n is hero]
                    if hero is not None
                    else eligible[:1]
                )
                # Подавленное называется поимённо: молчание, которое нельзя отличить от «нечего
                # слать», — это тихая деградация, даже когда оно по замыслу (I-6).
                if len(eligible) > len(to_send):
                    sent_ids = {id(n) for n, _, _ in to_send}
                    LOG.info(
                        "deep_batch_suppressed",
                        kept=[_compact_symbol(n.view.symbol) for n, _, _ in to_send],
                        suppressed=[
                            f"{_compact_symbol(n.view.symbol)}:{k}"
                            for n, _, k in eligible
                            if id(n) not in sent_ids
                        ],
                    )
            else:
                to_send = eligible
            for native, transition, kind in to_send:
                sym = _compact_symbol(native.view.symbol)
                # Per-(symbol, setup_kind) cooldown so distinct theses on one symbol each get
                # through, but the same thesis can't spam.
                cooldown_key = f"{sym}:{kind}"
                # ⚠ НАБЛЮДЕНИЕ, А НЕ ГЕЙТ. Пять гейтов по исходу в `track/_cooldowns.py`
                # не подключены (все ссылки — импорт и `__all__`, вызовов ноль), поэтому
                # единственный тормоз эмиссии — таймер `deep_cooldown_ok`, слепой к
                # результату. Включить их «как есть» нельзя: пороги откалиброваны на
                # 283 записях полосы МАНИПУЛЯЦИЙ, а `.claude/rules/prizrak.md` запрещает
                # переносить сюда чужие пороги и гейты. Здесь считается вердикт и пишется
                # в лог — чтобы к моменту, когда у призрака накопятся СВОИ закрытые сделки,
                # было по чему калибровать. См. `prizrak/arbiter.py::observe_outcome_gates`.
                observe_outcome_gates(sym, str(getattr(native.prizrak, "direction", "") or
                                               (native.prizrak.summary or {}).get("action") or ""))
                if deep_cooldown_ok(cooldown_key):
                    if await emitter.emit_deep(
                        broadcaster,
                        native,
                        cycle_peers=natives_only,
                        transition=transition,
                    ):
                        mark_deep_sent(cooldown_key)
        if send_telegram and broadcaster is not None and wait_candidates:
            for native in wait_candidates:
                try:
                    await send_analyst_change_telegram(broadcaster, native)
                except Exception:
                    LOG.exception("analyst_pinned_wait_card_failed",
                                  symbol=_compact_symbol(native.view.symbol))
        # ⚠ СОН ДЕДЛАЙННЫЙ, А НЕ ФИКСИРОВАННЫЙ. Здесь стояло `sleep(max(30.0, interval))`
        # ПОСЛЕ обхода, поэтому период = обход + interval, а не interval.
        #
        # ЗАМЕР (аудит 2026-08-01 по `data/analyst_ticks.jsonl`, 139 строк, 21 проход):
        # медиана периода по символам **371.3–390.3 с** при заявленных 300 (BTC 374.3,
        # XAU 390.3); медиана длительности обхода — **61.2 с** (min 41.9, max 175.4).
        # Арифметика сходится: 61.2 + 300 = 361.2 ≈ измеренному. Разрыв 24–30% и растёт
        # линейно с числом пиннутых символов, а кулдауны и `signal_queue_ttl_hours`
        # калибруются против числа, которого нет.
        #
        # Этот же класс в проекте УЖЕ починен — `engine/api.py::_poll_positioning` спит
        # `max(0, POLL_S - walk)` (правило `.claude/rules/engine-data-plane.md`, ловушка №1:
        # «период складывается из такта И обхода»). Здесь применено то же лечение.
        #
        # Просрочка не глотается: если обход длиннее интервала, период задаёт обход, и об
        # этом пишется в лог — иначе `interval_s` был бы величиной, которая не связывает,
        # и никто бы этого не увидел.
        walk_s = time.monotonic() - walk_started
        cadence_pause = interval - walk_s
        if cadence_pause <= 0.0:
            LOG.warning(
                "analyst_pinned_interval_overrun",
                walk_s=round(walk_s, 1),
                interval_s=interval,
                note="обход длиннее интервала — период эмиссии задаёт обход, а не настройка",
            )
        # ⚠ ВЫРАВНИВАНИЕ МОЖЕТ ТОЛЬКО ПРИБЛИЗИТЬ ПРОБУЖДЕНИЕ, НИКОГДА НЕ ОТДАЛИТЬ. Берётся
        # МИНИМУМ из штатного дедлайна и точки «граница бара + запас», поэтому оператор,
        # понизивший `interval_s`, получает ровно то, что просил, а не молча замедленную полосу.
        #
        # ⚠ И БУДИЛЬНИК ПО БАРУ НЕ ЗАМЕНЯЕТ ТАКТ, А ДОБАВЛЯЕТСЯ К НЕМУ. Проверено, а не
        # предположено: `prizrak/engines/activation.py::assess_activation` читает ЖИВУЮ цену, и
        # состояние `in_entry_zone` (то самое, что переводит сетап в «активирован») возникает
        # в СЕРЕДИНЕ бара. Полоса, разбуженная только закрытиями, пропускала бы вход в зону —
        # то есть «починка» лага стоила бы потери реакции на цену.
        #
        # Часы берутся из `clock`, а не из `time.time()`: сетка баров живёт в биржевом
        # времени, и с 2026-08-02 движок сводит с ним часы процесса (`Engine._sync_clock`).
        now_wall = clock.now_ms() / 1000.0
        bar_pause = _next_bar_wake_ts(now_wall) - now_wall
        pause = min(cadence_pause, bar_pause)
        LOG.info(
            "analyst_pinned_sleep",
            trigger="bar_close" if bar_pause <= cadence_pause else "cadence",
            pause_s=round(max(1.0, pause), 1),
            walk_s=round(walk_s, 1),
            bar_pause_s=round(bar_pause, 1),
            cadence_pause_s=round(cadence_pause, 1),
        )
        try:
            await asyncio.sleep(max(1.0, pause))
        except asyncio.CancelledError:
            break
    LOG.info("analyst_pinned_loop_stop")


__all__ = [
    "analyst_pinned_interval_s",
    "analyst_pinned_loop",
    "append_deep_tick_jsonl",
    "assemble_analyst_tick",
    "material_deep_change",
    "send_analyst_change_telegram",
]
