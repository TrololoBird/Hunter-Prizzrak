"""Persistent per-symbol baseline history (P0-A).

Rolling samples for quote_volume / oi / funding / trade_count survive
restarts under ``data/baseline/{SYMBOL}.json``. Z-scores use ``shared.mathlib``.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

import polars as pl

from hunt_core import clock, serde
from hunt_core.paths import BASELINE_DIR
from hunt_core.toolkit.robust_stats import robust_z

_MAX_SAMPLES = 288
_MIN_Z_N = 12
_TF_WINDOWS: tuple[tuple[str, int, float], ...] = (
    ("z_5m", 12, 1.0),
    ("z_1h", 60, 0.85),
    ("z_24h", 288, 0.65),
)


def _safe_float(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return v


@dataclass(slots=True)
class SymbolBaseline:
    symbol: str
    quote_volume: list[float] = field(default_factory=list)
    oi: list[float] = field(default_factory=list)
    funding: list[float] = field(default_factory=list)
    trade_count: list[float] = field(default_factory=list)
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, symbol: str) -> SymbolBaseline:
        def _series(key: str) -> list[float]:
            out: list[float] = []
            for item in raw.get(key) or []:
                v = _safe_float(item)
                if v is not None:
                    out.append(v)
            return out[-_MAX_SAMPLES:]

        return cls(
            symbol=symbol.upper(),
            quote_volume=_series("quote_volume"),
            oi=_series("oi"),
            funding=_series("funding"),
            trade_count=_series("trade_count"),
            updated_at=str(raw.get("updated_at") or ""),
        )


def _baseline_path(symbol: str) -> Any:
    return BASELINE_DIR / f"{symbol.upper()}.json"


def load_baseline(symbol: str) -> SymbolBaseline | None:
    p = _baseline_path(symbol)
    if not p.is_file():
        return None
    try:
        raw = serde.loads(p.read_text(encoding="utf-8"))
    except (OSError, serde.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    return SymbolBaseline.from_dict(raw, symbol=symbol)


def save_baseline(baseline: SymbolBaseline) -> None:
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    baseline.updated_at = clock.now_utc().isoformat()
    _baseline_path(baseline.symbol).write_text(
        serde.dumps_str(baseline.to_dict(), indent=True),
        encoding="utf-8",
    )


def _append(series: list[float], value: float | None) -> list[float]:
    if value is None or value <= 0:
        return series
    return (series + [value])[-_MAX_SAMPLES:]


def update_baseline_from_ticker(
    symbol: str,
    row: dict[str, Any],
    *,
    oi: float | None = None,
    funding: float | None = None,
) -> SymbolBaseline:
    sym = symbol.upper()
    base = load_baseline(sym) or SymbolBaseline(symbol=sym)
    qv = _safe_float(row.get("quote_volume") or row.get("quoteVolume"))
    tc = _safe_float(row.get("trade_count") or row.get("count"))
    oi_v = oi if oi is not None else _safe_float(row.get("oi") or row.get("openInterest"))
    fund = funding if funding is not None else _safe_float(row.get("funding_rate"))
    base.quote_volume = _append(base.quote_volume, qv)
    base.oi = _append(base.oi, oi_v)
    base.funding = _append(base.funding, fund)
    base.trade_count = _append(base.trade_count, tc)
    save_baseline(base)
    return base


def _z_for_window(series: list[float], window: int, *, min_n: int = _MIN_Z_N) -> float | None:
    if len(series) < min_n:
        return None
    tail = series[-window:] if window <= len(series) else series
    if len(tail) < min_n:
        return None
    return robust_z(pl.Series(tail), min_n=min_n)


def multi_tf_z(series: list[float], *, min_n: int = _MIN_Z_N) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for key, window, _weight in _TF_WINDOWS:
        z = _z_for_window(series, window, min_n=min_n)
        if z is None and window > min_n:
            z = _z_for_window(series, len(series), min_n=min_n)
        out[key] = z
    return out


def baseline_zscores(baseline: SymbolBaseline | None) -> dict[str, float | None]:
    if baseline is None:
        return {}
    qv_z = multi_tf_z(baseline.quote_volume)
    oi_z = multi_tf_z(baseline.oi)
    trade_z = multi_tf_z(baseline.trade_count)
    return {
        "volume_z": qv_z.get("z_24h"),
        "volume_z_5m": qv_z.get("z_5m"),
        "oi_z": oi_z.get("z_24h"),
        "oi_z_5m": oi_z.get("z_5m"),
        "trade_rate_z": trade_z.get("z_24h"),
    }


def batch_update_baselines(rows: list[dict[str, Any]], *, oi_by_sym: dict[str, float | None] | None = None) -> None:
    oi_map = oi_by_sym or {}
    for row in rows:
        sym = str(row.get("symbol") or "").strip().upper()
        if not sym:
            continue
        update_baseline_from_ticker(sym, row, oi=oi_map.get(sym))


__all__ = [
    "SymbolBaseline",
    "baseline_zscores",
    "batch_update_baselines",
    "load_baseline",
    "multi_tf_z",
    "save_baseline",
    "update_baseline_from_ticker",
]
