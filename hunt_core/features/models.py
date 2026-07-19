"""Typed feature panel (ADR-0004 S3) — the DERIVED-indicator half of the old god-object.

``features/`` becomes a pure ``MarketView → FeaturePanel`` transform. The market/positioning half
(funding/oi/mark/basis/book/cross/spot) already lives on :class:`~hunt_core.view.models.MarketView`;
what remains here is the *derived* layer — per-TF indicator summaries, regime labels, volume-profile
levels, the factor panel — computed from the (closed-only) kline frames.

Every summary field is ``Optional`` and set **only** when its source column is real: a warm-up or
absent value is ``None`` (fail-loud, I-6), never the fabricated ``rsi14=50`` / ``vol_ratio=1`` /
``delta_ratio=0.5`` the untyped ``tf_snapshot`` dict injected. ``extra="forbid"`` makes a phantom key a
construction error; ``frozen`` makes the panel an immutable value; ``arbitrary_types_allowed`` carries
the Polars frames. The scanner (Module 2) never consumes this — the module boundary holds by
construction (no shared dict).
"""
from __future__ import annotations

from collections.abc import Mapping

import polars as pl
from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, arbitrary_types_allowed=True)


class CandleShape(_Model):
    """Last closed candle geometry (was the ``tf_snapshot`` ``candle`` sub-dict)."""

    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    upper_wick_ratio: float | None = None
    lower_wick_ratio: float | None = None
    body_ratio: float | None = None
    bearish: bool | None = None
    bullish: bool | None = None


class TfSummary(_Model):
    """One timeframe's newest-closed-bar indicator readout (retyped ``tf_snapshot``, fail-loud None).

    ``idx=-1`` is the newest CLOSED bar (engine frames are closed-only, I-5) — no ``-2 if closed``
    shim. Every field is the real value or ``None``; the old fabricated defaults are gone.
    """

    close: float | None = None
    rsi14: float | None = None
    atr14: float | None = None
    atr_pct: float | None = None
    adx14: float | None = None
    ema20: float | None = None
    ema50: float | None = None
    ema200: float | None = None
    dist_ema20_pct: float | None = None
    macd_hist: float | None = None
    vol_ratio: float | None = None
    taker_imbalance_cusum: float | None = None
    delta_ratio: float | None = None
    bb_pct_b: float | None = None
    stoch_k: float | None = None
    supertrend_dir: int | None = None
    plus_di: float | None = None
    minus_di: float | None = None
    vwap_dev_atr: float | None = None
    bb_width_pctile: float | None = None
    obv_rising: bool | None = None
    squeeze_on: bool | None = None
    donchian_width_pct: float | None = None
    donchian_high20: float | None = None
    donchian_low20: float | None = None
    session_cvd: float | None = None
    session_cvd_prev: float | None = None
    rolling_cvd_24h: float | None = None
    prev_high: float | None = None
    close_time_ms: int | None = None
    trend: str | None = None
    # distribution stats (was **distribution_stats(df))
    return_zscore: float | None = None
    return_skew: float | None = None
    return_kurt: float | None = None
    # divergence flags (booleans — a computed False is real, not fabricated)
    bearish_rsi_div: bool | None = None
    bullish_rsi_div: bool | None = None
    bearish_macd_div: bool | None = None
    bullish_macd_div: bool | None = None
    rsi_trendline_bearish_break: bool | None = None
    rsi_trendline_bullish_break: bool | None = None
    bullish_hidden_stoch_div: bool | None = None
    bearish_hidden_stoch_div: bool | None = None
    candle: CandleShape | None = None


class VolumeProfile(_Model):
    """Volume-profile levels for one TF (was the ``poc_/vah_/val_`` scalars)."""

    poc: float | None = None
    vah: float | None = None
    val: float | None = None
    poc_direction: str | None = None


class Regime(_Model):
    """Derived market-regime labels (was the label half of ``regime_snapshot``; market data → view)."""

    market_regime: str | None = None
    bias_4h: str | None = None
    bias_1h: str | None = None
    structure_1h: str | None = None
    regime_4h: str | None = None
    regime_1h: str | None = None
    pump_cycle: str | None = None
    btc_beta_1h: float | None = None
    btc_corr_1h: float | None = None
    btc_decoupled_pump: bool | None = None
    btc_decoupled_dump: bool | None = None


class FactorPanel(_Model):
    """The cross-sectional factor row (was ``build_factor_panel``; reads MarketView + summaries)."""

    momentum_rsi15: float | None = None
    trend_adx1h: float | None = None
    flow_taker: float | None = None
    deriv_oi_z: float | None = None
    deriv_funding: float | None = None
    flow_cmf15: float | None = None


class Frames(_Model):
    """Prepared (indicator-enriched) closed-only OHLCV frames per TF; ``None`` = plane not ready."""

    m1: pl.DataFrame | None = None
    m5: pl.DataFrame | None = None
    m15: pl.DataFrame | None = None
    h1: pl.DataFrame | None = None
    h4: pl.DataFrame | None = None
    d1: pl.DataFrame | None = None
    w1: pl.DataFrame | None = None


class FeaturePanel(_Model):
    """Pure ``MarketView → FeaturePanel`` output: the derived-indicator layer, all fail-loud Optional."""

    symbol: str
    now_ms: int
    frames: Frames = Frames()
    tf: Mapping[str, TfSummary] = Field(default_factory=dict)
    vp: Mapping[str, VolumeProfile] = Field(default_factory=dict)
    regime: Regime = Regime()
    factors: FactorPanel = FactorPanel()
    not_ready: tuple[str, ...] = ()


__all__ = [
    "CandleShape",
    "TfSummary",
    "VolumeProfile",
    "Regime",
    "FactorPanel",
    "Frames",
    "FeaturePanel",
]
