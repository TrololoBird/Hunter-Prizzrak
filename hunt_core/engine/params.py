"""Grounded engine parameters — every value sourced (ADR-0002 §11).

No invented magic numbers. Each constant traces to one of three sources:

* **LIVE** — measured on the running bot (``data_plane_audit``);
* **DOC**  — Binance USDⓈ-M developer docs (authoritative cadence/limit);
* **PROJECT** — a named real repo (ccxt.pro, cryptofeed, python-binance, freqtrade, hummingbot).

The two event-driven bounds (trades/liquidations freshness, per-symbol emission lag) have **no
defensible constant** and are calibration parameters, not hardcoded facts — see the ``MEASURE_*``
markers. This is the "don't invent a 2.5% threshold" discipline made explicit.
"""
from __future__ import annotations

# --- WS cache sizes — PROJECT ccxt/pro/binance.py describe().options (eviction bound, not fetch) ---
OHLCV_LIMIT: int = 1000
TRADES_LIMIT: int = 1000
ORDER_BOOK_LIMIT: int = 1000
WATCH_ORDER_BOOK_RATE_MS: int = 100

# --- Staleness → force-reconnect (two layers) ---
# Layer 1: transport ping-pong (ccxt.pro `streaming` config; applied in exchanges.py). Binance pings
# every 3 min; ccxt closes the socket after keepAlive×maxPingPongMisses with no pong. 30s was too
# aggressive — event-loop saturation at startup caused self-close ~every 79s (main-client comment) —
# so 180000×3 ≈ 9-min BACKSTOP. The app watchdog (layer 2, 60s) is the real fast detector.
WS_KEEPALIVE_MS: int = 180_000  # DOC Binance 3-min ping; PROJECT main client (30s self-closed)
WS_MAX_PING_PONG_MISSES: int = 3  # PROJECT main client
# Layer 2: app no-message watchdog — PROJECT python-binance NO_MESSAGE_RECONNECT_TIMEOUT (60s),
# corroborated by LIVE (ws_last_msg_age median 0.3s, max 0.5s → 60s = 120-200× margin).
NO_MESSAGE_WATCHDOG_S: float = 60.0
WATCHDOG_INTERVAL_S: float = 30.0  # PROJECT cryptofeed timeout_interval
ORDERBOOK_RESNAPSHOT_S: float = 3600.0  # PROJECT hummingbot FULL_ORDER_BOOK_RESET_DELTA_SECONDS
WS_ROTATE_S: float = 86400.0  # DOC Binance 24h forced disconnect; PROJECT freqtrade rotates daily

# --- Reconnect backoff — PROJECT python-binance (jittered exponential; ccxt.pro does NOT back off) ---
BACKOFF_BASE_S: float = 0.1  # MIN_RECONNECT_WAIT
BACKOFF_CAP_S: float = 60.0  # MAX_RECONNECT_SECONDS
# On DDoSProtection/RateLimitExceeded (418/429) a short retry EXTENDS the IP ban — back off long.
RATE_LIMIT_BACKOFF_S: float = 60.0  # PROJECT cryptofeed 429 sleep

# --- REST poll cadences (the only recurring REST) ---
OI_CURRENT_POLL_S: float = 60.0  # PROJECT cryptofeed
# Cross-venue funding is an 8h number → 60s poll is ample; bound at 3× (DOC funding slow).
CROSS_FUNDING_POLL_S: float = 60.0
FRESH_CROSS_FUNDING_S: float = 180.0
# DOC: /futures/data/* are computed every 5 min — polling faster returns duplicates + burns budget.
FUTURES_DATA_POLL_S: float = 300.0
# Пауза МЕЖДУ отдельными запросами /futures/data внутри одного цикла опроса.
#
# Цикл делает 7 запросов на символ (open-interest + 5 статистик + базис). На семи пиннед-символах
# это 49 запросов подряд, вплотную, раз в 300 с. У /futures/data свой IP-лимит, гораздо более
# тесный, чем у основного fapi, и такой залп его срывает: живой прогон 2026-07-25 поймал -1003
# четыре раза за 83 минуты (22:16, 23:11, 23:16, 23:32) — всегда на fapiDataGetBasis, потому что
# он идёт последним и упирается в стену, а не потому, что он чем-то особенный. Бан глобальный
# (``rest._BAN_UNTIL_MS``), то есть валит ВСЕ планы позиционирования, а не только базис.
#
# ДОКУМЕНТИРОВАННЫЙ БЮДЖЕТ (сверено 2026-07-27, changelog Binance 2023-10-19): у `/futures/data/*`
# лимит **1000 запросов / 5 мин / IP**, то есть 200/мин = 3.33/с ⇒ минимальный интервал 300 мс.
# Документированный ВЕС этих эндпоинтов — 0: они метрятся отдельным счётчиком, а не общим
# 2400/мин, и заголовков `X-MBX-USED-WEIGHT-*` не возвращают ВООБЩЕ (замер 2026-07-27: все шесть
# отдают HTTP 200 с нулём x-mbx-заголовков, при том что /fapi/v1/klines отдаёт used-weight 60).
# Адаптивный бэк-офф по заголовкам здесь невозможен в принципе — только собственный счётчик.
#
# ⚠ И ccxt тут НЕ защита: implicit-методы `fapiData*` он троттлит, но против ОБЩЕГО ведра
# (`rateLimit=50 мс`, cost=1) — замер на нашем venv (ccxt 4.5.68, чистый троттлер без сети):
# 47.5 мс/запрос ⇒ 1263/мин, это **6.3× сверх бюджета**. `enableRateLimit=True` даёт ложную
# уверенность; единственные настоящие ворота — `rest.py::_FD_GATE` с этим интервалом.
#
# 1.2 с = 50 запросов/мин = **25% бюджета**, четырёхкратный запас. Обход одного круга: 6 запросов
# на символ (5 статистик + базис), ≈7.8 с с учётом RTT, то есть в 300-секундный такт помещается
# ~38 символов. Дальше такт не держится — это уже не тихая деградация, а WARNING
# `engine_positioning_walk_over_budget` из `api.py::_poll_positioning`, и бонд свежести растёт
# вместе с реальным периодом. Прежняя редакция обосновывала 1.2 с прикидкой «×49 ≈ 59 с» без
# единого числа из документации биржи — то есть окном без замера (I-7).
FUTURES_DATA_SPACING_S: float = 1.2

# --- Per-plane freshness bounds (Plane.read) ---
FRESH_BBO_S: float = 5.0  # LIVE age 0.4s + DOC bookTicker real-time
FRESH_DEPTH_S: float = 5.0  # LIVE age 0.4s, ttl_hint 5s
FRESH_MARK_S: float = 15.0  # DOC markPrice 3s cadence × 5
FRESH_TICKER_S: float = 10.0  # DOC miniTicker ~1s cadence × ~10 (24h rollup, not latency-critical)
# ⚠ Это ПОЛ бонда планов позиционирования, а не сам бонд. Настоящий бонд считает
# ``api.py::_poll_positioning`` от ИЗМЕРЕННОГО периода цикла (``max(POLL_S, время обхода)``
# × ``POSITIONING_BOUND_MARGIN``), потому что период складывается из такта И обхода, а обход
# растёт линейно с юниверсом. Константа 360 = 300 с такта × 1.2 — то же, что даст формула на
# юниверсе, влезающем в такт. Всё ещё читается напрямую в ``multi.py`` для кросс-венью OI/LS,
# где такт 60 с и запас шестикратный.
FRESH_FUTURES_DATA_S: float = 360.0  # DOC 5-min granularity + margin
# Запас бонда над измеренным периодом обновления. 1.25 = один пропущенный ответ ещё не
# «протухло», два подряд — уже да. Замер 2026-07-26: джиттер периода p90/median = 379.7/377.9
# = 1.005, то есть 1.25 покрывает его с большим запасом и остаётся чувствительным к реальному
# сбою поллера (при 300 с такта план становится stale через 375 с — быстрее одного цикла).
POSITIONING_BOUND_MARGIN: float = 1.25

# --- Наблюдаемость ---
# Порт локального экспортёра Prometheus (`engine/metrics.py::start_exporter`, только 127.0.0.1).
# 0 выключает. До 2026-07-26 экспортёра не было вообще: четыре метрики писались в реестр, из
# которого их некому было прочитать, при том что докстрока модуля обещала «внешний скрейпер».
METRICS_PORT: int = 9207
# Как часто пересчитывать и публиковать измеренный темп планов. Величина медленная (медиана по
# десяткам обновлений), самый редкий план обновляется раз в 300 с — чаще смысла нет.
CADENCE_PUBLISH_S: float = 120.0
FRESH_FUNDING_S: float = 8 * 3600.0 + 300.0  # DOC 8h settle + margin
# PROJECT freqtrade (observed post-close emission lag).
# ⚠ ЗАМЕР 2026-07-26/27, живой прогон. Величина ПОГРАНИЧНАЯ, и это надо знать прежде, чем её
# «чинить»: измеритель темпа даёт для `kline.1m` p90 интервала между слияниями 82.3 с при бонде
# 80 с (то есть `engine_plane_bound_tight` — истинное срабатывание, а не ложное). Но ВОЗРАСТ
# плана, то есть то, что реально видят потребители, за бондом не был НИ РАЗУ: 400 замеров,
# median 29.0 с, p90 54.0 с, p99 76.4 с, max 78.2 с — 0% за бондом.
# Расхождение объяснимо: интервал ловит каждый разрыв, возраст сэмплируется тиком раз в ~30 с и
# пики пропускает. Поднимать константу на выборке в 9 интервалов — ровно то «настраивание окна
# без замера», против которого стоит I-7. Менять только после набора интервалов ≥ нескольких
# сотен, и тогда ставить рядом новый замер.
_KLINE_EMISSION_LAG_S: float = 20.0


def fresh_kline_s(interval_s: float) -> float:
    """Freshness bound for a closed kline of ``interval_s`` — PROJECT freqtrade ``interval + 20s``.

    The only published freshness formula for closed-bar data; +20s absorbs the exchange's
    post-close emission lag.
    """
    return interval_s + _KLINE_EMISSION_LAG_S


# --- Connection limits — DOC Binance USDⓈ-M futures ---
MAX_STREAMS_PER_CONN: int = 1024
MAX_SUBSCRIBE_PER_S: int = 5
SHARD_STREAMS: int = 200  # practical shard size (PROJECT unicorn-binance)

# --- ⚠ MEASURE — no defensible constant; calibrate fail-loud from live, never hardcode ---
# Two bounds have NO published/measured source and MUST be calibrated from our own live logs, not
# baked in as constants (the "don't invent a 2.5% threshold" discipline):
#   * trades / liquidations freshness — event-driven, so silence ≠ staleness (a quiet symbol
#     legitimately has no trade). The transport watchdog (NO_MESSAGE_WATCHDOG_S, per-connection)
#     handles the dead-stream case; there is deliberately NO tight per-plane timeout for these.
#   * per-symbol WS-vs-exchange emission lag — freqtrade's +20s is a starting point, not ground truth.
# Until calibrated, trades/liq planes use NO_MESSAGE_WATCHDOG_S as a generous frozen-tape guard only.
