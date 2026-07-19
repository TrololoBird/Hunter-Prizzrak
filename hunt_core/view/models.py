"""``MarketView`` — the typed contract the rewritten modules consume (ADR-0004 spine).

Replaces the untyped ``dict[str, Any]`` row-dict that hosted the project's signature defect family
(phantom keys, falsy-zero ``or``-chains, name-lies, orphan fields). Built from ``engine.snapshot()``,
with the core invariant **presence ⟺ proven-fresh**: a field is non-``None`` iff the engine proved
its plane fresh at ``now_ms``. So I-6 (fail-loud) collapses into the type —

* a phantom key is a ``mypy`` error (frozen model, no ``.get()``);
* a falsy-zero ``or 0.0`` can't be written (no dict fallback; a genuine ``0.0`` passes through);
* a name-lie can't recur (the field name *is* the schema; prizrak/scanner own their own typed
  outputs, never a shared field);
* an orphan/unknown field is rejected at construction (``extra='forbid'``).

Derived layers (rsi/structure/mtf/fib/factor_panel) are deliberately NOT here — they are typed
outputs of ``features/`` produced *from* this view, which severs the old god-object.
"""
from __future__ import annotations

from collections.abc import Mapping

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from hunt_core.engine.state import NotReady

# Unified-timeframe → view attribute (the engine seeds these TFs; see api._DEFAULT_TFS).
_TF_ATTR: dict[str, str] = {
    "1m": "m1", "5m": "m5", "15m": "m15", "1h": "h1", "4h": "h4", "1d": "d1", "1w": "w1",
}


class _View(BaseModel):
    """Base for every view sub-model: frozen, strict, and closed to unknown keys."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",  # an unknown key is a construction error, not a silent orphan
        strict=True,  # no coercion: "0" never becomes 0 (I-6); int→float stays allowed
        arbitrary_types_allowed=True,  # for pl.DataFrame
    )


class Klines(_View):
    """Closed-only OHLCV frames per timeframe (I-5). ``None`` = the plane was not ready."""

    m1: pl.DataFrame | None = None
    m5: pl.DataFrame | None = None
    m15: pl.DataFrame | None = None
    h1: pl.DataFrame | None = None
    h4: pl.DataFrame | None = None
    d1: pl.DataFrame | None = None
    w1: pl.DataFrame | None = None

    def get(self, tf: str) -> pl.DataFrame | None:
        """The frame for a unified timeframe (``"4h"`` …), or ``None`` if absent/unknown TF."""
        attr = _TF_ATTR.get(tf)
        return getattr(self, attr) if attr is not None else None

    def require(self, tf: str) -> pl.DataFrame:
        """The frame for ``tf`` or raise :class:`NotReady` — mirrors ``MarketSnapshot.require``."""
        frame = self.get(tf)
        if frame is None:
            raise NotReady(f"kline.{tf}", "absent")
        return frame


class Book(_View):
    """Top-of-book + derived microstructure from the ``book`` plane (via ``toolkit`` book-math)."""

    bids: tuple[tuple[float, float], ...] | None = None
    asks: tuple[tuple[float, float], ...] | None = None
    bid: float | None = None
    ask: float | None = None
    depth_imbalance: float | None = None
    microprice_bias: float | None = None


class Derivs(_View):
    """Value-backed derivative planes + the derived funding stats (all fail-loud ``None``)."""

    mark: float | None = None
    index: float | None = None
    funding: float | None = None
    oi: float | None = None
    basis: float | None = None
    taker_5m: float | None = None
    global_ls_5m: float | None = None
    top_ls_acct_5m: float | None = None
    top_ls_pos_5m: float | None = None
    funding_zscore: float | None = None
    funding_trend: str | None = None


class Orderflow(_View):
    """Taker-flow / CVD / price-change / liquidation notional over the ``trades``/``liq`` planes.

    ``cvd_*`` is signed USDT **notional** (a deliberate unit change from the old base-qty ``ws_cvd``;
    sign preserved — recalibrate any magnitude threshold).
    """

    cvd_1m: float | None = None
    cvd_5m: float | None = None
    buy_ratio_30s: float | None = None
    buy_ratio_60s: float | None = None
    price_chg_1m: float | None = None
    price_chg_5m: float | None = None
    liq_long_5m: float | None = None
    liq_short_5m: float | None = None
    liq_score_5m: float | None = None


class Cross(_View):
    """Cross-venue divergence — per-venue dicts from ``MultiEngine.cross_*`` (each value fail-loud)."""

    funding: dict[str, float | None] = Field(default_factory=dict)
    open_interest: dict[str, float | None] = Field(default_factory=dict)
    long_short: dict[str, float | None] = Field(default_factory=dict)
    liq_notional: dict[str, dict[str, float] | None] = Field(default_factory=dict)


class Spot(_View):
    """Spot-vs-perp enrichment from ``SpotEngine.spot_enrichments`` (fail-loud ``None`` fields)."""

    spread_bps: float | None = None
    quote_volume_24h: float | None = None
    lead_return_1m: float | None = None
    taker_delta_usd: float | None = None
    taker_buy_ratio: float | None = None


class MarketView(_View):
    """The complete raw market contract for one symbol at ``now_ms`` — built from ``engine.snapshot``.

    ``last_price`` is the one always-present field (the engine requires the ticker/mark plane before a
    view is built); everything else is presence⟺fresh. ``not_ready`` carries the snapshot's failed
    planes for diagnostics/gating; ``plane_ages`` is the E7 freshness diagnostic.
    """

    symbol: str
    now_ms: int
    last_price: float
    price_source: str
    klines: Klines = Klines()
    book: Book = Book()
    derivs: Derivs = Derivs()
    orderflow: Orderflow = Orderflow()
    cross: Cross = Cross()
    spot: Spot = Spot()
    not_ready: tuple[str, ...] = ()
    plane_ages: Mapping[str, float] = Field(default_factory=dict)


__all__ = ["Klines", "Book", "Derivs", "Orderflow", "Cross", "Spot", "MarketView"]
