"""``EngineStreams`` — engine-backed drop-in for ``HuntCcxtStreams`` (ADR-0003 cutover, PIECE 1).

Presents only the WS surface consumers actually call (spec `docs/adr/0003-streams-wiring-spec.md` §1.1):
``snapshot`` + the ``live_*`` reads + ``trade_buffer``/``liquidation_buffers``/``closed_kline_overlay``.
Sourced from the push-state engine — no separate WS machinery. ``snapshot()`` deliberately does NOT
build the liquidation heatmap the old one did: recon proved **no consumer reads those fields** (the
maps path goes through ``liquidation_buffers()``), so reproducing them would be a name-lie (I-6).

Fail-loud throughout: an untracked/stale symbol yields ``None`` live fields + ``ws_connected=False``,
never a fabricated value. A genuine ``0.0`` (e.g. funding rate) passes through.
"""
from __future__ import annotations

import time
from collections import deque
from typing import Any

import structlog

from hunt_core.engine import params
from hunt_core.engine.api import Engine
from hunt_core.engine.liquidations import liquidation_notional
from hunt_core.engine.multi import MultiEngine
from hunt_core.engine.orderflow import price_change_pct, taker_flow

# Interim import location (E5b will move book-math to toolkit/; client_market.py imports the same way).
from hunt_core.market.client import depth_imbalance_from_book, microprice_bias_from_book

LOG = structlog.get_logger(__name__)

_LIQ_WINDOW_MS = 300_000  # the 300s window the old snapshot liquidation trio used


def _now_ms() -> int:
    return int(time.time() * 1000)


class EngineStreams:
    """Drop-in for ``HuntCcxtStreams`` — the consumed WS surface, engine-backed."""

    def __init__(self, engine: Engine, multi: MultiEngine) -> None:
        self._engine = engine
        self._multi = multi
        self._warned_set_symbols = False

    # --- lifecycle (drop-in no-ops: the engine is started/stopped by the plane) ---

    def set_symbols(self, symbols: Any, *, priority: Any = None) -> None:
        if not self._warned_set_symbols:
            LOG.debug("engine_streams_set_symbols_noop", note="warm set fixed at construction")
            self._warned_set_symbols = True

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    @property
    def kline_ws_enabled(self) -> bool:
        return True

    @property
    def cross_ws_connected(self) -> bool:
        return True

    # --- helpers ---

    def _trades(self, symbol: str) -> list[dict[str, Any]]:
        cache = (getattr(self._engine.exchange, "trades", {}) or {}).get(symbol)
        return list(cache) if cache else []

    def _liq_events(self, symbol: str, *, window_ms: int | None = None) -> list[dict[str, Any]]:
        cache = getattr(self._engine.exchange, "liquidations", None)
        if not cache:
            return []
        evs = [e for e in list(cache) if isinstance(e, dict) and e.get("symbol") == symbol]
        if window_ms is not None:
            cutoff = _now_ms() - window_ms
            evs = [e for e in evs if isinstance(e.get("timestamp"), (int, float)) and e["timestamp"] >= cutoff]
        return evs

    def _mark(self, symbol: str) -> dict[str, Any] | None:
        mk = self._engine.snapshot(symbol, ("mark",)).optional("mark")
        return mk if isinstance(mk, dict) else None

    def _book(self, symbol: str) -> dict[str, Any] | None:
        ob = self._engine.snapshot(symbol, ("book",)).optional("book")
        return ob if isinstance(ob, dict) else None

    @staticmethod
    def _l1(levels: Any) -> tuple[float | None, float | None]:
        """Top-of-book (price, qty) from a levels list, or (None, None)."""
        try:
            price, qty = float(levels[0][0]), float(levels[0][1])
            return price, qty
        except (TypeError, ValueError, IndexError):
            return None, None

    # --- live reads (fail-loud None) ---

    def live_ticker(self, symbol: str, *, max_age_s: float | None = None) -> dict[str, float] | None:
        tk = self._engine.snapshot(symbol, ("ticker",)).optional("ticker")
        if not isinstance(tk, dict):
            return None
        if max_age_s is not None and self._engine.plane_ages(symbol).get("ticker", 1e9) > max_age_s:
            return None
        out: dict[str, float] = {}
        for src, dst in (("last", "last"), ("quoteVolume", "quoteVolume"), ("percentage", "percentage"),
                         ("high", "high"), ("low", "low")):
            v = tk.get(src)
            if isinstance(v, (int, float)):
                out[dst] = float(v)
        return out or None

    def live_book(self, symbol: str) -> dict[str, Any] | None:
        ob = self._book(symbol)
        if ob is None:
            return None
        bids, asks = ob.get("bids") or [], ob.get("asks") or []
        bid, bid_qty = self._l1(bids)
        ask, ask_qty = self._l1(asks)
        return {
            "bid": bid, "ask": ask, "bid_qty": bid_qty, "ask_qty": ask_qty,
            "bids": bids, "asks": asks,
            "depth_imbalance": depth_imbalance_from_book(bid_qty=bid_qty, ask_qty=ask_qty, delta_ratio=None),
            "microprice_bias": microprice_bias_from_book(bid=bid, ask=ask, bid_qty=bid_qty, ask_qty=ask_qty, delta_ratio=None),
            "ts_ms": ob.get("timestamp"),
        }

    def live_bbo(self, symbol: str, *, max_age_s: float | None = None) -> dict[str, float] | None:
        if max_age_s is not None and self._engine.plane_ages(symbol).get("book", 1e9) > max_age_s:
            return None
        ob = self._book(symbol)
        if ob is None:
            return None
        bid, _ = self._l1(ob.get("bids") or [])
        ask, _ = self._l1(ob.get("asks") or [])
        if bid is None or ask is None or bid <= 0 or ask <= 0:
            return None
        return {"bid": bid, "ask": ask, "spread_pct": (ask - bid) / bid * 100.0}

    def live_funding(self, symbol: str, *, max_age_s: float | None = None) -> dict[str, Any] | None:
        mk = self._mark(symbol)
        if mk is None:
            return None
        if max_age_s is not None and self._engine.plane_ages(symbol).get("mark", 1e9) > max_age_s:
            return None
        return {
            "markPrice": mk.get("markPrice"),
            "indexPrice": mk.get("indexPrice"),
            "fundingRate": self._engine.snapshot(symbol, ("funding",)).optional("funding"),
            "ts_ms": mk.get("timestamp"),
        }

    def live_funding_cross(self, symbol: str, *, max_age_s: float = 900) -> dict[str, dict[str, float]] | None:
        cross = self._multi.cross_funding(symbol)
        out = {v: {"fundingRate": r} for v, r in cross.items() if r is not None and v != "binance"}
        return out or None

    def trade_buffer(self, symbol: str) -> deque[Any]:
        """Recent trades for the maps footprint/CVD. Returns a deque of ccxt trade dicts.

        NOTE (spec §1.3): the ``maps/`` feeder currently expects ``_AggPoint`` tuples; the exact shape
        bridge is finalized at the maps/ consumer-migration stage. Empty deque when absent (matches old).
        """
        return deque(self._trades(symbol))

    def liquidation_buffers(self, symbol: str | None = None) -> dict[str, deque[Any]]:
        """Per-venue recent liquidation events (ccxt dicts). Binance from the primary read-through,
        secondaries from ``multi.cross_liquidations``. Shape-bridge to ``(ts,sym,side,qty,price)`` tuples
        is finalized at the maps/ migration (spec §1.3). ``{"binance": deque()}`` when no events."""
        out: dict[str, deque[Any]] = {"binance": deque(self._liq_events(symbol) if symbol else [])}
        if symbol is not None:
            for venue, evs in self._multi.cross_liquidations(symbol).items():
                if venue != "binance" and evs:
                    out[venue] = deque(evs)
        return out

    def closed_kline_overlay(self, symbol: str, *, interval: str = "1m") -> dict[str, Any] | None:
        frame = self._engine.snapshot(symbol, (f"kline.{interval}",)).optional(f"kline.{interval}")
        if not isinstance(frame, list) or not frame:
            return None
        bar = frame[-1]  # engine frames are closed-only (I-5): [-1] is the newest CLOSED bar
        try:
            return {
                "ws_open_ms": int(bar[0]),
                "ws_interval": interval,
                "closed_bar": True,
                "close": float(bar[4]),
                "candle": {"open": float(bar[1]), "high": float(bar[2]), "low": float(bar[3]),
                           "close": float(bar[4]), "volume": float(bar[5])},
            }
        except (IndexError, TypeError, ValueError):
            return None

    # --- the consumed snapshot (spec §1.2) — no liquidation heatmap ---

    def snapshot(self, symbol: str) -> dict[str, Any]:
        now = _now_ms()
        ages = self._engine.plane_ages(symbol)
        trades = self._trades(symbol)
        ws_connected = bool(ages) and ages.get("book", 1e9) <= params.FRESH_DEPTH_S

        buy_30 = taker_flow(trades, window_ms=30_000, now_ms=now)["buy_ratio"]
        buy_60 = taker_flow(trades, window_ms=60_000, now_ms=now)["buy_ratio"]
        cvd_1m = taker_flow(trades, window_ms=60_000, now_ms=now)["delta"]
        cvd_5m = taker_flow(trades, window_ms=300_000, now_ms=now)["delta"]
        chg_1m = price_change_pct(trades, window_ms=60_000, now_ms=now)
        chg_5m = price_change_pct(trades, window_ms=300_000, now_ms=now)

        mk = self._mark(symbol)
        mark_px = mk.get("markPrice") if mk else None
        index_px = mk.get("indexPrice") if mk else None
        funding = self._engine.snapshot(symbol, ("funding",)).optional("funding")
        basis_bps = None
        if isinstance(mark_px, (int, float)) and isinstance(index_px, (int, float)) and index_px > 0:
            basis_bps = (mark_px - index_px) / index_px * 10_000.0

        ob = self._book(symbol)
        depth_imb = micro = None
        if ob is not None:
            bid, bid_qty = self._l1(ob.get("bids") or [])
            ask, ask_qty = self._l1(ob.get("asks") or [])
            depth_imb = depth_imbalance_from_book(bid_qty=bid_qty, ask_qty=ask_qty, delta_ratio=None)
            micro = microprice_bias_from_book(bid=bid, ask=ask, bid_qty=bid_qty, ask_qty=ask_qty, delta_ratio=None)

        liq = liquidation_notional(self._liq_events(symbol, window_ms=_LIQ_WINDOW_MS))
        liq_total = liq["total"]
        liq_score = round(liq["short"] / liq_total, 4) if liq_total > 0 else None

        live_ages = [a for k, a in ages.items() if k in ("book", "trades", "mark", "ticker")]

        return {
            "ws_connected": ws_connected,
            "agg_trade_source": "engine_taker_flow",
            "agg_trade_delta_30s": buy_30, "agg_trade_delta_60s": buy_60,
            "agg_trade_buy_ratio_30s": buy_30, "agg_trade_buy_ratio_60s": buy_60,
            "funding_live": funding, "live_funding_rate": funding,
            "mark_live": mark_px, "live_mark_price": mark_px,
            "live_index_price": index_px if isinstance(index_px, (int, float)) and index_px > 0 else None,
            "live_mark_ts_ms": (now - int(ages["mark"] * 1000)) if "mark" in ages else None,
            "basis_bps_live": basis_bps,
            "live_depth_imbalance": depth_imb, "live_microprice_bias": micro,
            "ws_cvd_1m": cvd_1m, "ws_cvd_5m": cvd_5m,
            "ws_price_chg_1m": chg_1m * 100.0 if chg_1m is not None else None,
            "ws_price_chg_5m": chg_5m * 100.0 if chg_5m is not None else None,
            "liquidation_score_5m": liq_score,
            "liquidation_long_notional_5m": liq["long"] if liq_total > 0 else None,
            "liquidation_short_notional_5m": liq["short"] if liq_total > 0 else None,
            "ws_last_msg_age_s": min(live_ages) if live_ages else None,
        }


__all__ = ["EngineStreams"]
