"""Native PRIZRAK assembly (ADR-0004 S8) — ``assemble_prizrak(view, maps) → PrizrakOutput``.

The engine-native replacement for ``entry.py::ensure_prizrak_verdict``: sources its inputs from a
:class:`MarketView` + :class:`MapBundle` instead of the row-dict, and calls the **unchanged** orchestrator
functions (``build_prizrak_signals`` / ``compute_prizrak_structure`` / ``compute_interest_zones``) so the
emitted candidates are byte-identical to today. The ~2450-line decision engine and all detectors are
untouched — this is a data-source swap at the seam, not a strategy change.

Klines come from the view as raw ``[ts_ms, o, h, l, c, v]`` bars (the shape every detector is validated
against); they are closed-only by engine guarantee (I-5), so the old ``drop_unclosed_ohlcv_tail`` crutch
is gone. marketcap/dominance stay off-process CoinGecko cache reads; the liq-reconcile context is read
from the typed MapBundle's derived features instead of ``row["market"]``.
"""
from __future__ import annotations

from typing import Any

import polars as pl
import structlog

from hunt_core.maps.engine import MapBundle, derive_map_features
from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.prizrak.liq_reconcile import compute_liquidation_factor
from hunt_core.prizrak.models import PrizrakOutput
from hunt_core.prizrak.orchestrator import (
    build_prizrak_signals,
    compute_interest_zones,
    compute_prizrak_structure,
)
from hunt_core.view.models import MarketView

LOG = structlog.get_logger("hunt.prizrak.assemble")

_TF_FIELD: dict[str, str] = {
    "1m": "m1", "5m": "m5", "15m": "m15", "1h": "h1", "4h": "h4", "1d": "d1", "1w": "w1"
}
_LIQ_KEYS = ("liq_cascade_risk", "liq_synthetic_only", "map_book_imbalance_1pct")
_MIN_BARS = 15


def _frame_to_bars(frame: pl.DataFrame) -> list[list[float]]:
    """Prepared/raw kline frame → raw ascending ``[open_ms, o, h, l, c, v]`` rows (detector shape)."""
    rows = frame.select(
        pl.col("time").dt.epoch(time_unit="ms").alias("t"),
        "open", "high", "low", "close", "volume",
    ).rows()
    return [list(r) for r in rows]


def _raw_klines_for_tiers(view: MarketView, cfg: PrizrakConfig) -> dict[str, list[list[float]]]:
    """Raw closed bars for every TF the config's scale tiers use (≥15 bars), from ``view.klines``."""
    need = {tf for tier in (cfg.intraday, cfg.meso, cfg.macro) for tf in tier.timeframes}
    out: dict[str, list[list[float]]] = {}
    for tf in need:
        field = _TF_FIELD.get(tf)
        frame = getattr(view.klines, field) if field else None
        if frame is None or frame.is_empty():
            continue
        bars = _frame_to_bars(frame)
        if len(bars) >= _MIN_BARS:
            out[tf] = bars
    return out


def _liq_context(maps: MapBundle | None, price: float) -> dict[str, Any] | None:
    """The 3 liq-reconcile keys off the typed MapBundle (was ``row["market"]`` after apply_map_bundle)."""
    if maps is None:
        return None
    feats = derive_map_features(maps, current_price=price)
    if not feats:
        return None
    return {key: feats.get(key) for key in _LIQ_KEYS}


def assemble_prizrak(
    view: MarketView, maps: MapBundle | None = None, *, cfg: PrizrakConfig | None = None
) -> PrizrakOutput:
    """Compute the full PRIZRAK verdict for ``view`` — typed output, orchestrator unchanged."""
    cfg = cfg or PrizrakConfig.load()
    price = view.last_price
    if price <= 0:
        return PrizrakOutput.empty(view.symbol)

    ohlcv_by_tf = _raw_klines_for_tiers(view, cfg)

    marketcap_series: list[list[float]] | None = None
    if cfg.marketcap_enabled:
        from hunt_core.prizrak.marketcap_source import read_cached_series

        marketcap_series = read_cached_series(view.symbol)
    dominance_changes: dict[str, float] | None = None
    if cfg.dominance_enabled:
        from hunt_core.prizrak.dominance_source import read_cached_changes_24h

        dominance_changes = read_cached_changes_24h()
    liq_context = _liq_context(maps, price) if cfg.liq_reconcile_enabled else None

    abstain: list[dict[str, Any]] = []
    candidates = build_prizrak_signals(
        ohlcv_by_tf, price=price, cfg=cfg, marketcap_series=marketcap_series,
        dominance_changes=dominance_changes, liq_context=liq_context, abstain_sink=abstain,
    )
    structure = compute_prizrak_structure(ohlcv_by_tf, cfg=cfg)
    zones = compute_interest_zones(ohlcv_by_tf, price=price, cfg=cfg)
    summary = max(candidates, key=lambda c: c["strength"]) if candidates else None

    if isinstance(summary, dict) and summary.get("liq_conflict"):
        rec = summary.get("liq_reconcile") or {}
        LOG.info(
            "prizrak_liq_conflict", symbol=view.symbol, direction=summary.get("action"),
            strength=summary.get("strength"),
            evidence=rec.get("evidence") if isinstance(rec, dict) else None,
        )

    bias_liq_conflict = _wait_bias_conflict(structure, liq_context, summary, cfg, view.symbol)
    return PrizrakOutput(
        symbol=view.symbol,
        signals=tuple(candidates),
        summary=summary,
        structure=structure,
        interest_zones=zones,
        abstain=tuple(abstain),
        bias_liq_conflict=bias_liq_conflict,
    )


def _wait_bias_conflict(
    structure: dict[str, Any],
    liq_context: dict[str, Any] | None,
    summary: dict[str, Any] | None,
    cfg: PrizrakConfig,
    symbol: str,
) -> dict[str, Any] | None:
    """WAIT-tick bias↔microstructure conflict (no candidate but a directional HTF bias) — was entry.py."""
    if summary is not None or liq_context is None:
        return None
    htf = structure.get("htf_bias") if isinstance(structure, dict) else None
    bias = str(htf.get("bias") or "").lower() if isinstance(htf, dict) else ""
    if bias not in {"long", "short"}:
        return None
    factor = compute_liquidation_factor(liq_context, direction=bias, cfg=cfg)
    if not factor.get("conflict"):
        return None
    evidence = factor.get("evidence") or []
    LOG.info("prizrak_bias_liq_conflict", symbol=symbol, bias=bias, evidence=evidence)
    return {"bias": bias, "evidence": evidence}


__all__ = ["assemble_prizrak"]
