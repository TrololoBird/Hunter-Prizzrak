"""Multi-venue orchestration — the primary Binance :class:`Engine` + secondary venues, all ccxt.

``MultiEngine`` runs the full Binance engine plus a lite ccxt.pro client per secondary venue
(OKX/Bybit/Bitget) and exposes the **cross-venue** positioning signals the strategies use, each a
uniform ccxt-native source polled on a slow cadence, fail-loud (a stale/absent venue reads ``None``,
never a fabricated value, so a divergence is only computed from fresh venues):

* **funding-rate** divergence — REST ``fetch_funding_rates`` (all four; only OKX streams it);
* **open-interest** divergence — REST ``fetch_open_interest`` (all four);
* **long/short account ratio** — unified ``fetch_long_short_ratio_history`` (all four; Binance maps
  it to the global-accounts ratio, matching the primary engine's ``global_ls_5m`` plane);
* **liquidations** — WS-only on **every** venue (``fetchLiquidations`` is unsupported everywhere:
  ``has=False`` on Binance, ``NotSupported`` on OKX/Bybit — verified 4.5.59). ``cross_liquidations``
  serves the **primary Binance** ``!forceOrder`` WS stream (read-through); secondary-venue WS
  liquidations (``watchLiquidations``: Binance/OKX universe, Bybit per-symbol, Bitget none) are a
  pending increment. There is **no public historical-liquidation backfill anywhere** — a liquidation
  window/zone exists only as the live-accumulated buffer and must be persisted to survive restarts.
  Notional is computed (``contracts × contractSize × price``) — never trusted from the payload.

Each secondary's optional signal is capability-gated at :meth:`start` (``has`` + listed markets), so
an unsupported venue is silently skipped rather than polled-and-failed every tick.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Sequence
from typing import Any

import structlog

from hunt_core.engine import exchanges, params, rest
from hunt_core.engine.api import Engine, _DEFAULT_TFS
from hunt_core.engine.liquidations import liquidation_notional, market_contract_size
from hunt_core.engine.state import MarketSnapshot, PlaneStamp, Source, SymbolState
from hunt_core.toolkit.book_math import depth_snapshot_from_book

LOG = structlog.get_logger(__name__)

_PRIMARY = "binance"


class MultiEngine:
    """Binance primary engine + secondary venues; adds a cross-venue funding view."""

    def __init__(
        self,
        symbols: Sequence[str],
        timeframes: Sequence[str] = _DEFAULT_TFS,
        secondaries: Sequence[str] = exchanges.SECONDARY_VENUES,
    ) -> None:
        self._symbols = list(symbols)
        self._primary = Engine(symbols, timeframes)
        self._secondary_ex = {v: exchanges.make_secondary(v) for v in secondaries}
        # per-venue cross-state: SymbolState holds value-backed planes (funding/oi/lsr/liq) per symbol.
        self._cross: dict[str, dict[str, SymbolState]] = {v: {} for v in secondaries}
        # per-venue optional-capability gate (filled at start from `has`) — poll only what's supported.
        self._cap: dict[str, dict[str, bool]] = {v: {} for v in secondaries}
        self._bg: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        await self._primary.start()
        for venue, ex in self._secondary_ex.items():
            try:
                await ex.load_markets()
            except Exception as exc:  # noqa: BLE001 — a dead secondary must not sink the primary
                LOG.warning("engine_secondary_load_failed", venue=venue, err=str(exc))
            has = getattr(ex, "has", {}) or {}
            # Only REST-pollable optional signals are gated here. Liquidations are WS-only on every
            # venue (fetchLiquidations unsupported), so there is no REST liq poll to gate.
            self._cap[venue] = {"lsr": bool(has.get("fetchLongShortRatioHistory"))}
        self._bg.append(asyncio.create_task(self._cross_loop(), name="engine_cross"))
        LOG.info(
            "multi_engine_started", primary=_PRIMARY, secondaries=list(self._secondary_ex), caps=self._cap
        )

    async def _cross_loop(self) -> None:
        """Poll the uniform cross-venue signals per secondary: funding + OI + long/short + liquidations.

        Bounds by data nature: funding uses the 180s cross-poll liveness bound
        (``FRESH_CROSS_FUNDING_S``, 3× the 60s cadence); OI and the long/short ratio are 5-min
        positioning stats and use the looser 360s bound. Every write is a real, fail-loud plane —
        a ``None`` from a poller is skipped, never stored as a fabricated value. (Liquidations are
        WS-only and not REST-polled here — see the class docstring.)
        """
        f_bound = int(params.FRESH_CROSS_FUNDING_S * 1000.0)
        oi_bound = int(params.FRESH_FUTURES_DATA_S * 1000.0)
        while True:
            for venue, ex in self._secondary_ex.items():
                now = int(time.time() * 1000)
                venue_state = self._cross[venue]
                cap = self._cap.get(venue, {})
                markets = getattr(ex, "markets", None) or {}
                rates = await rest.poll_funding_rates(ex, self._symbols)
                for sym, rate in rates.items():
                    venue_state.setdefault(sym, SymbolState(sym)).put_value(
                        "funding", rate, PlaneStamp(Source.REST_SEED, now, now, f_bound)
                    )
                for sym in self._symbols:
                    if sym not in markets:
                        continue
                    st = venue_state.setdefault(sym, SymbolState(sym))
                    oi = await rest.poll_open_interest(ex, sym)
                    if oi is not None:
                        st.put_value("oi", oi, PlaneStamp(Source.REST_SEED, now, now, oi_bound))
                    if cap.get("lsr"):
                        lsr = await rest.poll_long_short_ratio(ex, sym)
                        if lsr is not None:
                            st.put_value("lsr", lsr, PlaneStamp(Source.REST_SEED, now, now, oi_bound))
            await asyncio.sleep(params.CROSS_FUNDING_POLL_S)

    # --- consumer surface ---

    @property
    def primary(self) -> Engine:
        """The primary Binance :class:`Engine` (single-venue planes) — used by the cutover adapters."""
        return self._primary

    def snapshot(self, symbol: str, required: Sequence[str]) -> MarketSnapshot:
        """Primary (Binance) freshness-proven snapshot — unchanged single-venue contract."""
        return self._primary.snapshot(symbol, required)

    def _cross_value(self, symbol: str, plane: str, primary_plane: str) -> dict[str, float | None]:
        """``{venue: value|None}`` for a scalar plane across all venues — fail-loud per venue.

        Binance from the primary engine, secondaries from the cross poll. A stale/absent venue reads
        ``None`` (no data), never a fabricated value, so a divergence is only computed from fresh venues.
        """
        now = int(time.time() * 1000)
        out: dict[str, float | None] = {}
        pv = self._primary.snapshot(symbol, (primary_plane,)).optional(primary_plane)
        out[_PRIMARY] = float(pv) if isinstance(pv, (int, float)) else None
        for venue, states in self._cross.items():
            st = states.get(symbol)
            stamp = st.stamp_of(plane) if st is not None else None
            if st is None or stamp is None or stamp.stale_by(now) is not None:
                out[venue] = None
                continue
            v = st.value_of(plane)
            out[venue] = float(v) if isinstance(v, (int, float)) else None
        return out

    def cross_funding(self, symbol: str) -> dict[str, float | None]:
        """Fresh funding rate per venue: ``{venue: rate|None}`` (divergence signal, fail-loud)."""
        return self._cross_value(symbol, "funding", "funding")

    def cross_open_interest(self, symbol: str) -> dict[str, float | None]:
        """Fresh open interest per venue: ``{venue: oi|None}`` (fail-loud)."""
        return self._cross_value(symbol, "oi", "oi")

    def cross_long_short(self, symbol: str) -> dict[str, float | None]:
        """Fresh global long/short **account** ratio per venue: ``{venue: ratio|None}`` (fail-loud).

        Binance from the primary engine's ``global_ls_5m`` plane (``fapiData`` global-accounts ratio),
        secondaries from the unified ``fetch_long_short_ratio_history`` cross poll — the same metric,
        so the divergence is apples-to-apples.
        """
        return self._cross_value(symbol, "lsr", "global_ls_5m")

    def cross_liquidations(self, symbol: str) -> dict[str, list[dict[str, Any]] | None]:
        """Recent liquidation events per venue: ``{venue: [events]|None}`` (fail-loud).

        Binance from the primary WS ``!forceOrder`` stream (read-through). Secondary venues read a
        value-backed ``liq`` plane that a WS liquidation feeder fills — currently unwired, so OKX/Bybit
        return ``None`` (there is NO REST liquidation endpoint on any venue: ``fetchLiquidations`` is
        ``has=False``/``NotSupported`` everywhere; Bitget has no liquidation feed at all). The payload
        carries no reliable notional — pass each list through :meth:`cross_liquidation_notional`.

        Freshness semantics differ by source and a divergence consumer must window both consistently:
        the primary's ``liq`` plane is **event-stamped** (a quiet symbol reads ``None`` after
        ``NO_MESSAGE_WATCHDOG_S`` — no recent force-order), whereas a secondary's REST poll returns
        the venue's recent-liquidations window on every cycle (an empty poll is a fresh ``[]``, not
        ``None``). So ``None`` here means "no fresh data", not necessarily "no liquidations".
        """
        now = int(time.time() * 1000)
        out: dict[str, list[dict[str, Any]] | None] = {}
        prim = self._primary.snapshot(symbol, ("liq",)).optional("liq")
        out[_PRIMARY] = list(prim) if isinstance(prim, list) else None
        for venue, states in self._cross.items():
            st = states.get(symbol)
            stamp = st.stamp_of("liq") if st is not None else None
            if st is None or stamp is None or stamp.stale_by(now) is not None:
                out[venue] = None
                continue
            v = st.value_of("liq")
            out[venue] = list(v) if isinstance(v, list) else None
        return out

    def cross_liquidation_notional(self, symbol: str) -> dict[str, dict[str, float] | None]:
        """Per-venue liquidation notional ``{long, short, total}`` in USDT (fail-loud).

        Computes ``contracts × contractSize × price`` per event (the WS/REST ``baseValue``/
        ``quoteValue`` are unreliable), resolving each venue's market ``contractSize`` as the fallback.
        A venue with no fresh liquidation data → ``None``, never a fabricated zero-notional.
        """
        out: dict[str, dict[str, float] | None] = {}
        for venue, events in self.cross_liquidations(symbol).items():
            if events is None:
                out[venue] = None
                continue
            if venue == _PRIMARY:
                size = self._primary.contract_size(symbol)
            else:
                size = market_contract_size(self._secondary_ex[venue], symbol)
            out[venue] = liquidation_notional(events, contract_size=size)
        return out

    async def cross_orderbook(self, symbol: str, *, limit: int = 100) -> dict[str, dict[str, Any]]:
        """Per-venue depth snapshots (BASE-unit sizes) for cross-book aggregation — ``{venue: snap}``.

        Primary Binance from the WS ``book`` plane (read-through); secondaries via REST
        ``fetch_order_book``. Sizes are normalized ``×contractSize`` at the venue boundary — OKX's
        ``BTC-USDT-SWAP`` has ``contractSize=0.01`` while linear USDT is ``1.0``, so treating raw sizes
        as base units overstated OKX depth 100× and let the smallest book dominate the merged wall
        (documented cross-DOM defect). Fail-loud: a venue that fails / lacks the market / can't be sized
        is omitted (never a fabricated book), and each snapshot carries ``fetched_at_ms`` for the
        time-alignment the maps aggregator applies.
        """
        out: dict[str, dict[str, Any]] = {}
        prim = self._primary.snapshot(symbol, ("book",)).optional("book")
        if isinstance(prim, dict):
            bids = [(float(p), float(q)) for p, q in (prim.get("bids") or [])]
            asks = [(float(p), float(q)) for p, q in (prim.get("asks") or [])]
            if bids and asks:
                snap = depth_snapshot_from_book(bids, asks)
                snap["fetched_at_ms"] = float(int(time.time() * 1000))
                out[_PRIMARY] = snap

        async def _one(venue: str, ex: Any) -> tuple[str, dict[str, Any]] | None:
            markets = getattr(ex, "markets", None) or {}
            if symbol not in markets or not getattr(ex, "has", {}).get("fetchOrderBook"):
                return None
            try:
                ob = await ex.fetch_order_book(symbol, limit=min(100, max(5, int(limit))))
            except Exception as exc:  # noqa: BLE001 — a dead secondary must not sink the cross-book
                LOG.warning("engine_cross_book_failed", venue=venue, symbol=symbol, err=str(exc))
                return None
            cs = market_contract_size(ex, symbol)
            if cs is None or cs <= 0:
                return None  # cannot size correctly → skip, never assume 1.0 (would re-open the OKX 100× bug)
            bids = [(float(r[0]), float(r[1]) * cs) for r in (ob.get("bids") or []) if r]
            asks = [(float(r[0]), float(r[1]) * cs) for r in (ob.get("asks") or []) if r]
            if not bids or not asks:
                return None
            snap = depth_snapshot_from_book(bids, asks)
            snap["fetched_at_ms"] = float(int(time.time() * 1000))
            return venue, snap

        results = await asyncio.gather(
            *(_one(v, ex) for v, ex in self._secondary_ex.items()), return_exceptions=True
        )
        for r in results:
            if isinstance(r, tuple):
                out[r[0]] = r[1]
        return out

    async def close(self) -> None:
        for task in self._bg:
            task.cancel()
        await asyncio.gather(*self._bg, return_exceptions=True)
        await self._primary.close()
        for ex in self._secondary_ex.values():
            with contextlib.suppress(Exception):
                await ex.close()
