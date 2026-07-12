"""Feature vector extraction from prepared snapshots for the parquet feature lake."""
from __future__ import annotations



import json
import math
from dataclasses import asdict, dataclass, fields
from functools import lru_cache
from pathlib import Path
from typing import Any

import polars as pl

_REGISTRY_PATH = Path(__file__).resolve().parents[1] / "domain" / "feature_registry.json"

_FRAME_SOURCES = frozenset(
    {
        "close",
        "rsi14",
        "atr14",
        "atr_pct",
        "adx14",
        "ema20",
        "ema50",
        "ema200",
        "volume_ratio20",
        "macd_hist",
        "bb_pct_b",
        "bb_width",
        "supertrend_dir",
        "delta_ratio",
        "zscore30",
        "session_cvd",
        "rolling_cvd_24h",
    }
)


class FeatureExtractError(ValueError):
    """Raised when a required feature cannot be resolved from prepare outputs."""


@dataclass(slots=True)
class FeatureVector:
    """Scalar feature snapshot for one symbol × timeframe × tick."""

    symbol: str
    ts: str
    tf: str
    price: float
    close: float
    rsi14: float
    atr14: float
    adx14: float
    atr_pct: float | None = None
    ema20: float | None = None
    ema50: float | None = None
    ema200: float | None = None
    volume_ratio20: float | None = None
    macd_hist: float | None = None
    bb_pct_b: float | None = None
    bb_width: float | None = None
    supertrend_dir: float | None = None
    chg_24h_pct: float | None = None
    range_24h_pct: float | None = None
    oi: float | None = None
    oi_change_pct: float | None = None
    oi_slope_5m: float | None = None
    funding_rate: float | None = None
    ls_ratio: float | None = None
    global_ls_ratio: float | None = None
    depth_imbalance: float | None = None
    microprice_bias: float | None = None
    basis_pct: float | None = None
    premium_zscore_5m: float | None = None
    liquidation_score: float | None = None
    delta_ratio: float | None = None
    zscore30: float | None = None
    session_cvd: float | None = None
    rolling_cvd_24h: float | None = None
    oi_acceleration: float | None = None
    funding_velocity: float | None = None
    poc_migration_1h: float | None = None
    poc_migration_4h: float | None = None
    va_contraction: float | None = None
    liquidity_void_path: float | None = None
    lifecycle_phase: str | None = None
    lifecycle_bias: str | None = None
    market_regime: str | None = None
    closed_bar: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@lru_cache(maxsize=1)
def load_feature_registry() -> dict[str, Any]:
    raw = _REGISTRY_PATH.read_text(encoding="utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, dict) or "features" not in payload:
        msg = f"invalid feature registry: {_REGISTRY_PATH}"
        raise FeatureExtractError(msg)
    return payload


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, pl.Series):
        if value.len() == 0:
            return None
        value = value[-1]
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(parsed) or math.isinf(parsed):
        return None
    return parsed


_TREND_POSITIVE = frozenset({"up", "rising", "long", "bull", "bullish", "positive"})
_TREND_NEGATIVE = frozenset({"down", "falling", "short", "bear", "bearish", "negative"})
_TREND_NEUTRAL = frozenset({"flat", "neutral", "none", "sideways", "stable"})


def _encode_signed_label(value: Any) -> float | None:
    """Encode categorical trend/migration labels as -1/0/+1 for lake numeric cols."""
    if value is None:
        return None
    numeric = _coerce_float(value)
    if numeric is not None:
        return numeric
    text = str(value).strip().lower()
    if text in _TREND_POSITIVE:
        return 1.0
    if text in _TREND_NEGATIVE:
        return -1.0
    if text in _TREND_NEUTRAL:
        return 0.0
    return None


def _scalar_bool(value: Any, *, default: bool = False) -> bool:
    """Bool coercion safe for polars Series (never use ``if series``)."""
    if value is None:
        return default
    if isinstance(value, pl.Series):
        if value.len() == 0:
            return default
        value = value[-1]
    if isinstance(value, (bool, int, float)):
        return bool(value)
    return default


def _require_float(value: Any, *, field: str, symbol: str, tf: str) -> float:
    parsed = _coerce_float(value)
    if parsed is None:
        msg = f"required feature {field!r} missing for {symbol} tf={tf}"
        raise FeatureExtractError(msg)
    return parsed


def _closed_frame_block(prepared: Any, row: dict[str, Any], tf: str) -> dict[str, Any]:
    """Prefer grace-closed bar snapshot; never use a forming bar for fusion."""
    closed_key = f"{tf}_closed"
    snap = ((row.get("timeframes") or {}).get(closed_key) or {})
    if isinstance(snap, dict) and snap.get("status") != "empty" and _scalar_bool(snap.get("closed_bar")):
        # Merge _FRAME_SOURCES columns missing from snapshot (e.g. zscore30, delta_ratio)
        if prepared is not None:
            work = getattr(prepared, f"work_{tf}", None)
            if work is not None and getattr(work, "height", 0) >= 2:
                cols = getattr(work, "columns", [])
                missing = {n for n in _FRAME_SOURCES if n not in snap and n in cols}
                if missing:
                    snap = dict(snap)
                    for name in missing:
                        try:
                            snap[name] = work.item(-2, name)
                        except (pl.exceptions.PolarsError, IndexError, TypeError, ValueError):
                            pass
        return snap
    if prepared is not None:
        work = getattr(prepared, f"work_{tf}", None)
        if work is not None and getattr(work, "height", 0) >= 2:
            cols = getattr(work, "columns", [])
            idx = -2
            out: dict[str, Any] = {"closed_bar": True}
            for name in _FRAME_SOURCES:
                if name in cols:
                    try:
                        out[name] = work.item(idx, name)
                    except (pl.exceptions.PolarsError, IndexError, TypeError, ValueError):
                        continue
            if len(out) > 1:
                return out
    return {}


def _frame_block(
    prepared: Any,
    row: dict[str, Any],
    tf: str,
    *,
    require_closed: bool = False,
) -> dict[str, Any]:
    if require_closed:
        closed = _closed_frame_block(prepared, row, tf)
        if closed:
            return closed
        return {"status": "empty", "closed_bar": False}
    if prepared is not None:
        attr = f"work_{tf}"
        work = getattr(prepared, attr, None)
        if work is not None and not getattr(work, "is_empty", lambda: True)():
            cols = getattr(work, "columns", [])
            out: dict[str, Any] = {}
            for name in _FRAME_SOURCES:
                if name in cols:
                    try:
                        out[name] = work.item(-1, name)
                    except (pl.exceptions.PolarsError, IndexError, TypeError, ValueError):
                        continue
            if out:
                out.setdefault("closed_bar", False)
                return out
    snap = ((row.get("timeframes") or {}).get(tf) or {})
    if isinstance(snap, dict):
        snap.setdefault("closed_bar", _scalar_bool(snap.get("closed_bar")))
        return snap
    return {}


_ROW_FIRST_KEYS = frozenset({"oi_current", "oi_change_pct", "oi_slope_5m", "funding_rate"})


def _prepared_value(prepared: Any, row: dict[str, Any], attr: str) -> Any:
    aliases = {
        "oi_current": ("oi",),
        "oi_change_pct": ("oi_chg_1h", "oi_change_pct"),
        "funding_rate": ("funding", "funding_rate"),
        "ls_ratio": ("ls_1h", "ls_ratio"),
        "global_ls_ratio": ("global_ls", "global_ls_ratio"),
        "depth_imbalance": ("depth", "depth_imbalance"),
        "microprice_bias": ("microprice", "microprice_bias"),
        "basis_pct": ("basis", "basis_pct"),
        "premium_zscore_5m": ("premium_zscore_5m",),
        "liquidation_score": ("liquidation_score_5m", "liquidation_score"),
    }
    market = row.get("market") or row.get("positioning") or {}
    if isinstance(market, dict) and attr in _ROW_FIRST_KEYS:
        for key in aliases.get(attr, (attr,)):
            if key in market and market[key] is not None:
                return market[key]
    if prepared is not None:
        val = getattr(prepared, attr, None)
        if val is not None:
            return val
    if not isinstance(market, dict):
        return None
    for key in aliases.get(attr, (attr,)):
        if key in market and market[key] is not None:
            return market[key]
    return None


def build_feature_vector(
    prepared: Any,
    row: dict[str, Any],
    *,
    symbol: str,
    tf: str,
    require_closed: bool = False,
) -> FeatureVector:
    """Extract registry-backed features from prepare outputs; fail loud on required gaps."""
    sym = (symbol or str(row.get("symbol") or "")).upper()
    if not sym:
        raise FeatureExtractError("symbol is required for feature vector extraction")

    ts = row.get("ts")
    if not ts:
        raise FeatureExtractError(f"ts missing for feature vector extraction: {sym}")

    frame = _frame_block(prepared, row, tf, require_closed=require_closed)
    if frame.get("status") == "empty":
        raise FeatureExtractError(f"timeframe frame empty for {sym} tf={tf}")
    if require_closed and not _scalar_bool(frame.get("closed_bar")):
        raise FeatureExtractError(f"closed bar unavailable for {sym} tf={tf}")

    lifecycle = row.get("lifecycle") or {}
    regime = row.get("regime") or {}

    vector_kwargs: dict[str, Any] = {
        "symbol": sym,
        "ts": str(ts),
        "tf": tf,
        "price": _require_float(row.get("price"), field="price", symbol=sym, tf=tf),
        "close": _require_float(frame.get("close"), field="close", symbol=sym, tf=tf),
        "rsi14": _require_float(frame.get("rsi14"), field="rsi14", symbol=sym, tf=tf),
        "atr14": _require_float(frame.get("atr14"), field="atr14", symbol=sym, tf=tf),
        "adx14": _require_float(frame.get("adx14"), field="adx14", symbol=sym, tf=tf),
    }

    for name in _FRAME_SOURCES:
        if name in {"close", "rsi14", "atr14", "adx14"}:
            continue
        vector_kwargs[name] = _coerce_float(frame.get(name))

    vector_kwargs["chg_24h_pct"] = _coerce_float(row.get("chg_24h_pct"))
    vector_kwargs["range_24h_pct"] = _coerce_float(row.get("range_24h_pct"))
    vector_kwargs["oi"] = _coerce_float(_prepared_value(prepared, row, "oi_current"))
    vector_kwargs["oi_change_pct"] = _coerce_float(
        _prepared_value(prepared, row, "oi_change_pct")
    )
    vector_kwargs["oi_slope_5m"] = _coerce_float(_prepared_value(prepared, row, "oi_slope_5m"))
    vector_kwargs["funding_rate"] = _coerce_float(_prepared_value(prepared, row, "funding_rate"))
    vector_kwargs["ls_ratio"] = _coerce_float(_prepared_value(prepared, row, "ls_ratio"))
    vector_kwargs["global_ls_ratio"] = _coerce_float(
        _prepared_value(prepared, row, "global_ls_ratio")
    )
    vector_kwargs["depth_imbalance"] = _coerce_float(
        _prepared_value(prepared, row, "depth_imbalance")
    )
    vector_kwargs["microprice_bias"] = _coerce_float(
        _prepared_value(prepared, row, "microprice_bias")
    )
    vector_kwargs["basis_pct"] = _coerce_float(_prepared_value(prepared, row, "basis_pct"))
    vector_kwargs["premium_zscore_5m"] = _coerce_float(
        _prepared_value(prepared, row, "premium_zscore_5m")
    )
    vector_kwargs["liquidation_score"] = _coerce_float(
        _prepared_value(prepared, row, "liquidation_score")
    )
    _mkt = row.get("market")
    market = _mkt if isinstance(_mkt, dict) else {}
    oi_slope = _coerce_float(_prepared_value(prepared, row, "oi_slope_5m"))
    oi_chg = _coerce_float(_prepared_value(prepared, row, "oi_change_pct"))
    if oi_slope is not None and oi_chg is not None:
        vector_kwargs["oi_acceleration"] = oi_slope - (oi_chg / 100.0)
    elif oi_slope is not None:
        vector_kwargs["oi_acceleration"] = oi_slope
    funding_trend = market.get("funding_trend")
    if funding_trend is None and prepared is not None:
        funding_trend = getattr(prepared, "funding_trend", None)
    vector_kwargs["funding_velocity"] = _encode_signed_label(funding_trend)
    vector_kwargs["poc_migration_1h"] = _encode_signed_label(market.get("map_poc_migration_1h"))
    vector_kwargs["poc_migration_4h"] = _encode_signed_label(market.get("map_poc_migration_4h"))
    vector_kwargs["va_contraction"] = _coerce_float(market.get("map_vp_va_contraction"))
    if vector_kwargs["va_contraction"] is None and market.get("map_vp_va_contraction") is not None:
        vector_kwargs["va_contraction"] = 1.0 if bool(market.get("map_vp_va_contraction")) else 0.0
    vector_kwargs["liquidity_void_path"] = _coerce_float(market.get("map_void_above_pct"))
    vector_kwargs["lifecycle_phase"] = (
        str(lifecycle.get("phase")) if lifecycle.get("phase") is not None else None
    )
    vector_kwargs["lifecycle_bias"] = (
        str(lifecycle.get("recommended_bias"))
        if lifecycle.get("recommended_bias") is not None
        else None
    )
    vector_kwargs["market_regime"] = (
        str(regime.get("market_regime")) if regime.get("market_regime") is not None else None
    )
    vector_kwargs["closed_bar"] = _scalar_bool(frame.get("closed_bar"), default=not require_closed)

    registry = load_feature_registry().get("features") or {}
    missing_required: list[str] = []
    for field_name, meta in registry.items():
        if not isinstance(meta, dict) or not meta.get("required"):
            continue
        if field_name not in vector_kwargs or vector_kwargs[field_name] is None:
            missing_required.append(field_name)
    if missing_required:
        raise FeatureExtractError(
            f"required registry features missing for {sym} tf={tf}: {sorted(missing_required)}"
        )

    allowed = {f.name for f in fields(FeatureVector)}
    filtered = {k: v for k, v in vector_kwargs.items() if k in allowed}
    return FeatureVector(**filtered)


# ── Optional feature provenance metadata ─────────────────────────────────────


@dataclass(slots=True)
class FeatureProvenance:
    """Optional metadata attached to a feature extraction for replay/debug.

    Every field is optional — the hot path stays lean; provenance is opt-in.
    Through 6+ months of operation, these fields are the most useful for
    investigating strange signals and model degradation.
    """
    producer_version: str = ""
    feature_version: str = ""
    event_id: str = ""
    event_time: str = ""
    processing_time: str = ""
    source_latency_ms: int = 0
    compute_latency_ms: float = 0.0


def record_feature_provenance(
    vector: FeatureVector,
    *,
    producer_version: str = "",
    feature_version: str = "",
    event_id: str = "",
    event_time: str = "",
    processing_time: str = "",
    source_latency_ms: int = 0,
    compute_latency_ms: float = 0.0,
) -> FeatureProvenance:
    """Attach optional provenance metadata to a FeatureVector extraction.

    The provenance is a separate object — the vector remains lean for hot-path.
    """
    return FeatureProvenance(
        producer_version=producer_version,
        feature_version=feature_version,
        event_id=event_id,
        event_time=event_time,
        processing_time=processing_time,
        source_latency_ms=source_latency_ms,
        compute_latency_ms=compute_latency_ms,
    )


__all__ = [
    "FeatureExtractError",
    "FeatureProvenance",
    "FeatureVector",
    "build_feature_vector",
    "load_feature_registry",
    "record_feature_provenance",
]
