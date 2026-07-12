"""Live-row adapter — bridges the pipeline's ``row["timeframes"][tf]["ohlcv"]`` (list of
dicts) into the raw-CCXT-row shape (``[ts,o,h,l,c,v]``) every prizrak detector is built
and validated against (this session's ONDO/BTC live comparisons used exactly that
shape). Kept as the only place that knows about the live row schema, so the detectors
stay testable against plain historical data independent of the running pipeline.
"""
from __future__ import annotations

from typing import Any

from hunt_core.prizrak.config import PrizrakConfig


def _dict_bars_to_rows(bars: list[dict[str, Any]]) -> list[list[float]]:
    out: list[list[float]] = []
    for b in bars:
        if not isinstance(b, dict):
            continue
        try:
            ts = float(b.get("close_time_ms") or b.get("ts") or 0)
            o, h, l, c = float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"])
        except (KeyError, TypeError, ValueError):
            continue
        v = float(b.get("volume") or 0.0)
        out.append([ts, o, h, l, c, v])
    return out


def row_ohlcv_by_tf(row: dict[str, Any], *, cfg: PrizrakConfig | None = None) -> dict[str, list[list[float]]]:
    """Pull raw-row OHLCV for every timeframe any prizrak scale tier needs."""
    cfg = cfg or PrizrakConfig.load()
    _tf_raw = row.get("timeframes")
    tf: dict[str, Any] = _tf_raw if isinstance(_tf_raw, dict) else {}
    needed: set[str] = set()
    for tier in (cfg.intraday, cfg.meso, cfg.macro):
        needed.update(tier.timeframes)

    out: dict[str, list[list[float]]] = {}
    for t in needed:
        for key in (f"{t}_closed", t):
            block = tf.get(key) if isinstance(tf, dict) else None
            if not isinstance(block, dict):
                continue
            bars = block.get("ohlcv")
            if isinstance(bars, list) and len(bars) >= 15:
                out[t] = _dict_bars_to_rows(bars)
                break
    return out


__all__ = ["row_ohlcv_by_tf"]
