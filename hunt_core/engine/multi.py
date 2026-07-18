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
        self._bg.append(asyncio.create_task(self._cross_funding_loop(), name="engine_cross_funding"))
        LOG.info("multi_engine_started", primary=_PRIMARY, secondaries=list(self._secondary_ex))

    async def _cross_funding_loop(self) -> None:
        bound = int(params.FRESH_CROSS_FUNDING_S * 1000.0)
        while True:
            for venue, ex in self._secondary_ex.items():
                rates = await rest.poll_funding_rates(ex, self._symbols)
                now = int(time.time() * 1000)
                venue_state = self._cross[venue]
                for sym, rate in rates.items():
                    st = venue_state.setdefault(sym, SymbolState(sym))
                    st.put_value("funding", rate, PlaneStamp(Source.REST_SEED, now, now, bound))
            await asyncio.sleep(params.CROSS_FUNDING_POLL_S)

    # --- consumer surface ---

    def snapshot(self, symbol: str, required: Sequence[str]) -> MarketSnapshot:
        """Primary (Binance) freshness-proven snapshot — unchanged single-venue contract."""
        return self._primary.snapshot(symbol, required)

    def cross_funding(self, symbol: str) -> dict[str, float | None]:
        """Fresh funding rate per venue for a symbol: ``{venue: rate|None}``.

        Binance comes from the primary engine's WS mark stream; secondaries from the REST poll. A
        venue whose datum is absent or stale reads ``None`` (no data), never a fabricated rate — so
        a divergence signal is only computed from venues that are actually fresh.
        """
        now = int(time.time() * 1000)
        out: dict[str, float | None] = {}
        primary = self._primary.snapshot(symbol, ("funding",))
        val = primary.optional("funding")
        out[_PRIMARY] = float(val) if isinstance(val, (int, float)) else None
        for venue, states in self._cross.items():
            st = states.get(symbol)
            stamp = st.stamp_of("funding") if st is not None else None
            if st is None or stamp is None or stamp.stale_by(now) is not None:
                out[venue] = None
                continue
            fv = st.value_of("funding")
            out[venue] = float(fv) if isinstance(fv, (int, float)) else None
        return out

    async def close(self) -> None:
        for task in self._bg:
            task.cancel()
        await asyncio.gather(*self._bg, return_exceptions=True)
        await self._primary.close()
        for ex in self._secondary_ex.values():
            with contextlib.suppress(Exception):
                await ex.close()
