"""Multi-venue orchestration — the primary Binance :class:`Engine` + secondary venues, all ccxt.

``MultiEngine`` runs the full Binance engine plus a lite ccxt.pro client per secondary venue
(OKX/Bybit/Bitget) and exposes the **cross-venue** signal the strategies use: funding-rate
divergence across venues. Funding is an 8h number and not every secondary streams it (OKX does,
Bybit/Bitget don't), so the uniform ccxt-native source is REST ``fetch_funding_rates`` polled on a
slow cadence — one deterministic source per venue, fail-loud (a stale/absent venue reads ``None``,
never a fabricated rate).

Cross-venue liquidations (OKX/Bybit WS, gated) are a planned second increment.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Sequence

import structlog

from hunt_core.engine import exchanges, params, rest
from hunt_core.engine.api import Engine, _DEFAULT_TFS
from hunt_core.engine.state import MarketSnapshot, PlaneStamp, Source, SymbolState

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
        # per-venue cross-state: SymbolState holds a value-backed "funding" plane per symbol.
        self._cross: dict[str, dict[str, SymbolState]] = {v: {} for v in secondaries}
        self._bg: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        await self._primary.start()
        for venue, ex in self._secondary_ex.items():
            try:
                await ex.load_markets()
            except Exception as exc:  # noqa: BLE001 — a dead secondary must not sink the primary
                LOG.warning("engine_secondary_load_failed", venue=venue, err=str(exc))
        self._bg.append(asyncio.create_task(self._cross_loop(), name="engine_cross"))
        LOG.info("multi_engine_started", primary=_PRIMARY, secondaries=list(self._secondary_ex))

    async def _cross_loop(self) -> None:
        """Poll the uniform cross-venue positioning signals per secondary: funding + open interest."""
        f_bound = int(params.FRESH_CROSS_FUNDING_S * 1000.0)
        oi_bound = int(params.FRESH_FUTURES_DATA_S * 1000.0)
        while True:
            for venue, ex in self._secondary_ex.items():
                now = int(time.time() * 1000)
                venue_state = self._cross[venue]
                markets = getattr(ex, "markets", None) or {}
                rates = await rest.poll_funding_rates(ex, self._symbols)
                for sym, rate in rates.items():
                    venue_state.setdefault(sym, SymbolState(sym)).put_value(
                        "funding", rate, PlaneStamp(Source.REST_SEED, now, now, f_bound)
                    )
                for sym in self._symbols:
                    if sym not in markets:
                        continue
                    oi = await rest.poll_open_interest(ex, sym)
                    if oi is not None:
                        venue_state.setdefault(sym, SymbolState(sym)).put_value(
                            "oi", oi, PlaneStamp(Source.REST_SEED, now, now, oi_bound)
                        )
            await asyncio.sleep(params.CROSS_FUNDING_POLL_S)

    # --- consumer surface ---

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

    async def close(self) -> None:
        for task in self._bg:
            task.cancel()
        await asyncio.gather(*self._bg, return_exceptions=True)
        await self._primary.close()
        for ex in self._secondary_ex.values():
            with contextlib.suppress(Exception):
                await ex.close()
