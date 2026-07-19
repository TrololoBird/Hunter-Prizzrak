"""``compute_features(view) → FeaturePanel`` (ADR-0004 S3) — the pure native features entry.

Replaces the ``prepare_symbol`` god-object path for the engine tick: reads the closed-only kline
frames off a :class:`MarketView`, runs the unchanged pure-Polars ``_prepare_frame`` indicator pipeline
per TF, and projects the result onto the frozen typed :class:`FeaturePanel`. No ``client``, no ``pack``,
no ``ws_snap`` — the market/positioning half is already on the view (``derivs``/``orderflow``/``book``/
``cross``/``spot``).

``regime``/``vp``/``factors`` are filled in follow-up (they need cross-TF + view context); this entry
already produces the frames + per-TF summaries the tick and deliver layers read.
"""
from __future__ import annotations

import polars as pl

from hunt_core.features.models import FactorPanel, FeaturePanel, Frames, TfSummary
from hunt_core.features.prepare_columns import resolve_prepare_groups_for_symbol
from hunt_core.features.prepare_frame import _prepare_frame
from hunt_core.features.summary import tf_summary
from hunt_core.view.models import MarketView

_TF_TO_FIELD: dict[str, str] = {
    "1m": "m1", "5m": "m5", "15m": "m15", "1h": "h1", "4h": "h4", "1d": "d1", "1w": "w1"
}
# TFs that get the heavier divergence/trendline analysis (mirrors the tick's pinned tf_snapshot flags).
_RICH_TFS = frozenset({"15m", "1h", "4h"})


def _binance_id(symbol: str) -> str:
    """Unified ``BASE/QUOTE:SETTLE`` → binance id ``BASEQUOTE`` (so PINNED_SYMBOLS matching works)."""
    return symbol.split(":", 1)[0].replace("/", "")


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _last_finite(frame: pl.DataFrame | None, col: str) -> float | None:
    """Newest non-null value of ``col`` (the closed bar), or ``None`` — fail-loud, no fabrication."""
    if frame is None or col not in frame.columns or frame.height == 0:
        return None
    val = frame[col].drop_nulls().tail(1)
    if val.len() == 0:
        return None
    out = float(val.item())
    return out if out == out else None  # NaN-guard


def _build_factors(
    view: MarketView, tf15: TfSummary | None, tf1h: TfSummary | None, frame15: pl.DataFrame | None
) -> FactorPanel:
    """Cross-sectional factor row from the view + summaries (was ``build_factor_panel(row)``).

    Each factor is set only when its source is real (I-6). ``deriv_oi_z`` needs an OI-history z-score
    (the derived-stats-over-history layer) which is not a per-tick plane — left ``None`` until that
    refresher lands, never a fabricated 0.
    """
    rsi = tf15.rsi14 if tf15 else None
    adx = tf1h.adx14 if tf1h else None
    taker = view.derivs.taker_5m
    funding = view.derivs.funding
    cmf = _last_finite(frame15, "cmf20")
    return FactorPanel(
        momentum_rsi15=_clamp((50.0 - rsi) / 50.0, -1.0, 1.0) if rsi is not None else None,
        trend_adx1h=_clamp(adx / 50.0, 0.0, 1.0) if adx is not None else None,
        flow_taker=_clamp((taker - 1.0) * 2.0, -1.0, 1.0) if taker is not None else None,
        # funding fraction → percentage-points ×50 (matches the legacy funding_pct·50 slope).
        deriv_funding=_clamp(funding * 5000.0, -1.0, 1.0) if funding is not None else None,
        flow_cmf15=_clamp(cmf, -1.0, 1.0) if cmf is not None else None,
        deriv_oi_z=None,  # needs OI-history z-score refresher (tracked)
    )


def compute_features(view: MarketView) -> FeaturePanel:
    """Pure ``MarketView → FeaturePanel``: prepared indicator frames + typed per-TF summaries."""
    frames: dict[str, pl.DataFrame] = {}
    summaries: dict[str, TfSummary] = {}
    groups = resolve_prepare_groups_for_symbol(_binance_id(view.symbol))  # pinned→full, alts→lean
    for tf, field in _TF_TO_FIELD.items():
        raw: pl.DataFrame | None = getattr(view.klines, field)
        if raw is None or raw.is_empty():
            continue
        prepared = _prepare_frame(raw, active_groups=groups)  # I-5: closed-only frames in
        frames[field] = prepared
        rich = tf in _RICH_TFS
        summary = tf_summary(prepared, rsi_trendline=rich, hidden_stoch_div=rich)
        if summary is not None:
            summaries[tf] = summary
    factors = _build_factors(view, summaries.get("15m"), summaries.get("1h"), frames.get("m15"))
    return FeaturePanel(
        symbol=view.symbol,
        now_ms=view.now_ms,
        frames=Frames(**frames),
        tf=summaries,
        factors=factors,
        not_ready=view.not_ready,
    )


__all__ = ["compute_features"]
