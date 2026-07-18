"""Pure derived funding statistics over the settled funding-history records (ADR-0003 E4a).

Replaces the ``client.get_cached_funding_rate_zscore`` / ``get_cached_funding_trend`` /
``get_cached_funding_recent_extreme`` computations that lived (wrongly) inside the transport client.
Input is the ccxt ``fetchFundingRateHistory`` list (each record: ``fundingRate`` + ``timestamp``),
which ``engine/rest.py::fetch_funding_history`` supplies — the same settled-history source the old
client used (``_funding_history_cache``), NOT a rolling WS buffer.

Semantics match the old client exactly (verified against `market/client.py:1435-1491`), with the one
falsy-zero bug fixed: the old ``recent_extreme`` did ``float(row.get("fundingRate") or 0.0)`` which
fabricates ``0.0`` for a missing rate — here a missing/non-finite rate is skipped fail-loud. All pure.
"""
from __future__ import annotations

import math
import statistics
from typing import Any

_RISING_FRACTION = 0.75  # ≥75% of steps up → "rising" (old client threshold)


def _finite(x: Any) -> float | None:
    try:
        value = float(x)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _rates(records: list[dict[str, Any]] | None) -> list[float]:
    """Finite ``fundingRate`` values in order, skipping unparseable records (fail-loud)."""
    out: list[float] = []
    for r in records or []:
        if not isinstance(r, dict):
            continue
        rate = _finite(r.get("fundingRate"))
        if rate is not None:
            out.append(rate)
    return out


def _record_ms(r: dict[str, Any]) -> int | None:
    """Settlement time (ms): unified ``timestamp``, falling back to raw ``info.fundingTime``."""
    ts = r.get("timestamp")
    if isinstance(ts, (int, float)) and ts > 0:
        return int(ts)
    info = r.get("info")
    raw = info.get("fundingTime") if isinstance(info, dict) else None
    try:
        v = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def funding_trend(
    records: list[dict[str, Any]] | None, *, min_records: int = 3, window: int = 4
) -> str | None:
    """``"rising"``/``"falling"``/``"flat"`` over the last ``window`` settled rates, else ``None``.

    ``None`` when fewer than ``min_records`` finite rates (can't judge a trend) — never a fabricated
    "flat". Matches the old client: ≥75% of step-diffs up → rising, ≥75% down → falling.
    """
    rates = _rates(records)
    if len(rates) < min_records:
        return None
    tail = rates[-window:]
    diffs = [b - a for a, b in zip(tail, tail[1:])]
    steps = len(diffs)
    if steps == 0:
        return None
    ups = sum(1 for d in diffs if d > 0)
    downs = sum(1 for d in diffs if d < 0)
    if ups >= steps * _RISING_FRACTION:
        return "rising"
    if downs >= steps * _RISING_FRACTION:
        return "falling"
    return "flat"


def funding_zscore(records: list[dict[str, Any]] | None, *, min_records: int = 6) -> float | None:
    """Z-score of the latest settled rate vs the history's mean/σ (sample σ, ddof=1), else ``None``.

    ``None`` when fewer than ``min_records`` finite rates. A degenerate σ (≤1e-12) → ``0.0`` (the
    latest sits at the mean), matching the old client — this is a real reading, not a fabrication.
    """
    rates = _rates(records)
    if len(rates) < min_records:
        return None
    mean = statistics.fmean(rates)
    stdev = statistics.stdev(rates)  # ddof=1
    if stdev <= 1e-12:
        return 0.0
    return (rates[-1] - mean) / stdev


def funding_recent_extreme(
    records: list[dict[str, Any]] | None, *, now_ms: int, max_age_hours: float = 48.0
) -> tuple[float, float] | None:
    """The largest-magnitude settled rate within ``max_age_hours`` as ``(rate, age_hours)``, else ``None``.

    Fail-loud: a record missing a finite rate or a valid settlement time is skipped (the old client's
    ``or 0.0`` fabricated a zero rate here — fixed). ``None`` when nothing falls inside the window.
    """
    max_age_ms = max(0.0, float(max_age_hours)) * 3_600_000.0
    candidates: list[tuple[float, float]] = []
    for r in records or []:
        if not isinstance(r, dict):
            continue
        rate = _finite(r.get("fundingRate"))
        ts = _record_ms(r)
        if rate is None or ts is None:
            continue
        age_ms = max(0.0, float(now_ms - ts))
        if age_ms <= max_age_ms:
            candidates.append((rate, age_ms / 3_600_000.0))
    if not candidates:
        return None
    return max(candidates, key=lambda item: abs(item[0]))


__all__ = ["funding_trend", "funding_zscore", "funding_recent_extreme"]
