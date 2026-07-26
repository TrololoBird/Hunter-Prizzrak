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


def oi_change(series: list[float] | None, *, window: int = 1) -> float | None:
    """OI change over ``window`` steps as a FRACTION (``series[-1]/series[-1-window] - 1``).

    ``window=1`` (default) is last-vs-previous and matches ``client.fetch_open_interest_change``
    exactly — that legacy contract is preserved. Needs ``window+1`` points and a strictly positive
    baseline (the division/zero guard). A shorter or degenerate series is "no data" (``None``),
    never a fabricated ``0.0`` change (I-6).

    The value is a FRACTION (``0.05`` = +5%); callers wanting percent multiply by 100. Getting this
    wrong is a live defect class here — a fraction under a ``*_pct`` name once produced a negative
    stop price — so the unit is restated at every call site.

    Choosing ``window`` (invariant I-7 — a window without a measurement is a magic number): the only
    consumer, ``maps/oi.py::classify_oi_regime``, bands OI at **±15%** against price at ±5%. Measured
    2026-07-26 over 8 majors on 1h-period history: hour-over-hour OI moved at most **0.33%**, 24h at
    most **2.54%**, 48h at most **1.95%**. So ``window=1`` against a 15% band is 45× out of reach and
    could never bind; the 24h pairing is the only one on the right order of magnitude for what the
    regime is meant to catch (a squeeze / new-money ignition, a rare event by construction).
    """
    if window < 1:
        return None
    if not series or len(series) < window + 1:
        return None
    prev = _finite(series[-1 - window])
    last = _finite(series[-1])
    if prev is None or last is None or prev <= 0:
        return None
    return last / prev - 1.0


__all__ = ["oi_change", "oi_series"]
