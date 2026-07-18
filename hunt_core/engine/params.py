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
# DOC: /futures/data/* are computed every 5 min — polling faster returns duplicates + burns budget.
FUTURES_DATA_POLL_S: float = 300.0

# --- Per-plane freshness bounds (Plane.read) ---
FRESH_BBO_S: float = 5.0  # LIVE age 0.4s + DOC bookTicker real-time
FRESH_DEPTH_S: float = 5.0  # LIVE age 0.4s, ttl_hint 5s
FRESH_MARK_S: float = 15.0  # DOC markPrice 3s cadence × 5
FRESH_TICKER_S: float = 10.0  # DOC miniTicker ~1s cadence × ~10 (24h rollup, not latency-critical)
FRESH_FUTURES_DATA_S: float = 360.0  # DOC 5-min granularity + margin
FRESH_FUNDING_S: float = 8 * 3600.0 + 300.0  # DOC 8h settle + margin
_KLINE_EMISSION_LAG_S: float = 20.0  # PROJECT freqtrade (observed post-close emission lag)


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
