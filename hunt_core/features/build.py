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

from hunt_core.features.models import FeaturePanel, Frames, TfSummary
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
    return FeaturePanel(
        symbol=view.symbol,
        now_ms=view.now_ms,
        frames=Frames(**frames),
        tf=summaries,
        not_ready=view.not_ready,
    )


__all__ = ["compute_features"]
