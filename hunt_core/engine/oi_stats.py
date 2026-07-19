"""Pure derived open-interest statistics over a sumOpenInterest series (ADR-0004 S8).

Replaces ``client.fetch_open_interest_change`` (market/client.py:853-890) — the last-vs-previous OI
%-change that lived (wrongly) inside the transport client. Input is the ``sumOpenInterest`` series
(base-asset OI, oldest→newest) that ``engine/rest.py::fetch_futures_data_series`` supplies from
``/futures/data/openInterestHist`` (``fapiDataGetOpenInterestHist`` → ``sumOpenInterest``) — the SAME
value the old client read via ccxt's ``fetch_open_interest_history`` (which parses ``sumOpenInterest``
into ``openInterestAmount``). Semantics match the old client exactly: ``series[-1]/series[-2] - 1``,
``None`` when there are <2 finite points or the previous value is ≤ 0 (division guard).

This is the raw-data GAP the S8 map flagged as blocking the maps/features clusters (``oi_change_pct``,
structural_forecast.py:21). Pure + additive — nothing consumes it until the features cutover wires it.
Fail-loud (I-6): a short/degenerate series yields ``None`` (нет данных), never a fabricated ``0.0``.
"""
from __future__ import annotations

import math
from typing import Any


def _finite(x: Any) -> float | None:
    try:
        value = float(x)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def oi_series(rows: list[dict[str, Any]] | None, *, key: str = "sumOpenInterest") -> list[float]:
    """Finite OI values (oldest→newest) from raw ``/futures/data/openInterestHist`` rows.

    Convenience for callers holding the raw ccxt rows rather than the pre-parsed float series;
    skips any row whose ``key`` is missing/unparseable (fail-loud), never fabricating a point.
    """
    out: list[float] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        value = _finite(row.get(key))
        if value is not None:
            out.append(value)
    return out


def oi_change(series: list[float] | None) -> float | None:
    """Last-vs-previous OI change as a FRACTION (``series[-1]/series[-2] - 1``), or ``None``.

    Matches ``client.fetch_open_interest_change`` exactly: needs ≥2 finite points and a strictly
    positive previous value (the division/zero guard). A shorter or degenerate series is "no data"
    (``None``), never a fabricated ``0.0`` change (I-6). The value is a fraction (e.g. ``0.05`` = +5%);
    callers that want percent multiply by 100, as the legacy tick did.
    """
    if not series or len(series) < 2:
        return None
    prev = _finite(series[-2])
    last = _finite(series[-1])
    if prev is None or last is None or prev <= 0:
        return None
    return last / prev - 1.0


__all__ = ["oi_change", "oi_series"]
