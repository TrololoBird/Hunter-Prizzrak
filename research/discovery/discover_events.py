"""
Event discovery — find significant market events using multiple independent methods.

Each event records WHY it was detected (trigger_reasons).
This enables analyzing not just the events, but the effectiveness of each detection method.

Detection methods:
  1. volatility_breakout  — move exceeds N × ATR or rolling std
  2. percentile_move      — move exceeds Nth percentile of historical distribution
  3. multi_candle_impulse — sustained move across multiple consecutive candles
  4. directional_run      — long unbroken sequence of same-direction candles
  5. volume_spike         — volume exceeds N × rolling average
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from research.paths import cache_path, get_active_version, report_path  # noqa: E402

# ── config ──────────────────────────────────────────────────
SYMBOLS = [
    "TIA/USDT:USDT",
    "EVAA/USDT:USDT",
    "TAC/USDT:USDT",
    "XAN/USDT:USDT",
    "HMSTR/USDT:USDT",
    "LAB/USDT:USDT",
]

TIMEFRAMES = ["5m", "15m", "1h", "4h", "1d"]

# detection thresholds
# 40% floor scoped to real "мощные" manipulations (verified manually against
# 1h swing highs/lows over the last 14 days: 4 events ≥100%, ~6-7 more in the
# 40-93% band — 10-11 total genuine standalone pumps/dumps across 6 symbols,
# not the 2-8% noise a 10% floor was catching).
MIN_MAGNITUDE_PCT = 40.0
ATR_MULTIPLIER = 3.5            # volatility_breakout: move > 3.5 × ATR
PERCENTILE_THRESHOLD = 98       # percentile_move: above 98th percentile
MULTI_CANDLE_BARS = 3           # multi_candle_impulse: >= 3 consecutive bars
MULTI_CANDLE_TOTAL_PCT = 8.0    #   with total move >= 8%
DIRECTIONAL_RUN_BARS = 10       # directional_run: >= 10 same-direction bars
VOLUME_SPIKE_RATIO = 3.5        # volume_spike: volume > 3.5 × rolling avg
ROLLING_WINDOW = 30             # rolling std / ATR window
MERGE_WINDOW_BARS = 20          # merge hits within this gap
OVERLAP_THRESHOLD = 0.30        # merge events with >=30% overlap
MAX_EVENT_BARS = 60             # cap event size to prevent trend-as-event

# The manipulations under study happened in the last EVENT_LOOKBACK_DAYS days
# (confirmed by the operator); older volatility on these coins is unrelated
# noise. Preparation for a manipulation can start earlier, which is why
# extract_windows still looks BARS_BEFORE bars back from event_start_ts —
# this cutoff only bounds which bars can be an event *start*.
EVENT_LOOKBACK_DAYS = 14


# ── helpers ─────────────────────────────────────────────────
def _tf_to_ms(tf: str) -> int:
    mapping = {
        "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
        "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
        "6h": 21_600_000, "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000,
        "3d": 259_200_000, "1w": 604_800_000, "1M": 2_592_000_000,
    }
    return mapping.get(tf, 3_600_000)


def _ms_to_iso(ms: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ms / 1000))


# ── detection methods ───────────────────────────────────────
@dataclass
class DetectionHit:
    """A single detection from one method at one bar index."""
    method: str
    bar_idx: int
    magnitude: float
    detail: str = ""


def detect_volatility_breakout(
    close: np.ndarray, high: np.ndarray, low: np.ndarray,
    atr: np.ndarray, min_mag: float, multiplier: float,
) -> list[DetectionHit]:
    """Move exceeds multiplier × ATR from local origin."""
    hits = []
    n = len(close)
    for i in range(ROLLING_WINDOW, n):
        origin = close[i - 1]
        local_atr = atr[i]
        if local_atr <= 0:
            continue
        # check forward until move exceeds threshold or reverses
        peak_up = origin
        peak_dn = origin
        for j in range(i, min(i + 50, n)):
            if close[j] > peak_up:
                peak_up = close[j]
            if close[j] < peak_dn:
                peak_dn = close[j]
            up_pct = (peak_up - origin) / origin * 100
            dn_pct = (origin - peak_dn) / origin * 100
            if up_pct > multiplier * local_atr / origin * 100:
                hits.append(DetectionHit(
                    method="volatility_breakout",
                    bar_idx=i,
                    magnitude=up_pct,
                    detail=f"up {up_pct:.1f}% > {multiplier}×ATR",
                ))
                break
            if dn_pct > multiplier * local_atr / origin * 100:
                hits.append(DetectionHit(
                    method="volatility_breakout",
                    bar_idx=i,
                    magnitude=dn_pct,
                    detail=f"dn {dn_pct:.1f}% > {multiplier}×ATR",
                ))
                break
    return hits


def detect_percentile_move(
    close: np.ndarray, min_mag: float, percentile: int,
) -> list[DetectionHit]:
    """Move exceeds Nth percentile of rolling historical distribution."""
    hits = []
    n = len(close)
    if n < ROLLING_WINDOW * 2:
        return hits

    # compute rolling pct changes
    pct_changes = np.abs(np.diff(close) / close[:-1] * 100)

    for i in range(ROLLING_WINDOW, len(pct_changes)):
        window = pct_changes[max(0, i - ROLLING_WINDOW * 3):i]
        if len(window) < 10:
            continue
        threshold = np.percentile(window, percentile)
        if pct_changes[i] >= threshold and pct_changes[i] >= min_mag:
            hits.append(DetectionHit(
                method="percentile_move",
                bar_idx=i,
                magnitude=float(pct_changes[i]),
                detail=f"{pct_changes[i]:.1f}% >= P{percentile} ({threshold:.1f}%)",
            ))
    return hits


def detect_multi_candle_impulse(
    close: np.ndarray, min_bars: int, min_total_pct: float,
) -> list[DetectionHit]:
    """Sustained move across multiple consecutive candles in same direction."""
    hits = []
    n = len(close)
    i = 0
    while i < n - min_bars:
        direction = 1 if close[i + 1] > close[i] else (-1 if close[i + 1] < close[i] else 0)
        if direction == 0:
            i += 1
            continue

        run_start = i
        j = i + 1
        while j < n:
            if direction == 1 and close[j] > close[j - 1]:
                j += 1
            elif direction == -1 and close[j] < close[j - 1]:
                j += 1
            else:
                break

        run_len = j - run_start
        if run_len >= min_bars:
            total_pct = abs(close[j - 1] - close[run_start]) / close[run_start] * 100
            if total_pct >= min_total_pct:
                hits.append(DetectionHit(
                    method="multi_candle_impulse",
                    bar_idx=run_start,
                    magnitude=total_pct,
                    detail=f"{'up' if direction == 1 else 'dn'} {run_len} bars, {total_pct:.1f}%",
                ))
        i = max(j, i + 1)
    return hits


def detect_directional_run(
    close: np.ndarray, min_bars: int,
) -> list[DetectionHit]:
    """Long unbroken sequence of same-direction closes."""
    hits = []
    n = len(close)
    i = 0
    while i < n - min_bars:
        if close[i + 1] == close[i]:
            i += 1
            continue
        direction = 1 if close[i + 1] > close[i] else -1
        j = i + 1
        while j < n:
            if direction == 1 and close[j] > close[j - 1]:
                j += 1
            elif direction == -1 and close[j] < close[j - 1]:
                j += 1
            else:
                break

        run_len = j - i
        if run_len >= min_bars:
            total_pct = abs(close[j - 1] - close[i]) / close[i] * 100
            hits.append(DetectionHit(
                method="directional_run",
                bar_idx=i,
                magnitude=total_pct,
                detail=f"{'up' if direction == 1 else 'dn'} {run_len} bars, {total_pct:.1f}%",
            ))
        i = max(j, i + 1)
    return hits


def detect_volume_spike(
    volume: np.ndarray, close: np.ndarray, min_ratio: float,
) -> list[DetectionHit]:
    """Volume exceeds min_ratio × rolling average, coinciding with price move."""
    hits = []
    n = len(volume)
    if n < ROLLING_WINDOW:
        return hits

    vol_avg = np.convolve(volume, np.ones(ROLLING_WINDOW) / ROLLING_WINDOW, mode="same")

    for i in range(ROLLING_WINDOW, n - 1):
        if vol_avg[i] <= 0:
            continue
        ratio = volume[i] / vol_avg[i]
        if ratio >= min_ratio:
            # check if there was a price move
            pct = abs(close[i + 1] - close[i]) / close[i] * 100 if close[i] > 0 else 0
            if pct >= 0.5:  # at least 0.5% move with volume spike
                hits.append(DetectionHit(
                    method="volume_spike",
                    bar_idx=i,
                    magnitude=ratio,
                    detail=f"vol {ratio:.1f}×avg, {pct:.1f}% move",
                ))
    return hits


# ── main discovery engine ───────────────────────────────────
def compute_atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, window: int = 14) -> np.ndarray:
    """Average True Range."""
    n = len(high)
    tr = np.zeros(n)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
    atr = np.zeros(n)
    atr[:window] = tr[:window]
    for i in range(window, n):
        atr[i] = (atr[i - 1] * (window - 1) + tr[i]) / window
    return atr


def merge_hits_to_events(
    hits: list[DetectionHit],
    close: np.ndarray,
    ts: np.ndarray,
    tf_ms: int,
    symbol: str,
    tf: str,
    merge_window_bars: int = 20,
) -> list[dict]:
    """
    Merge overlapping detections into unified events.

    Strategy:
    1. Build preliminary events from each hit (with estimated end bar).
    2. Merge any two events whose time ranges overlap by >= 50%.
    3. Collect all trigger_reasons from merged events.
    """
    if not hits:
        return []

    # sort by bar index
    hits.sort(key=lambda h: h.bar_idx)

    # step 1: build preliminary events from each hit
    prelim_events: list[dict] = []
    for h in hits:
        # estimate event end: scan forward to find where move retraces
        start_idx = h.bar_idx
        start_price = float(close[start_idx])
        peak_idx = start_idx
        peak_price = start_price

        for j in range(start_idx, min(start_idx + 100, len(close))):
            if abs(close[j] - start_price) > abs(peak_price - start_price):
                peak_price = close[j]
                peak_idx = j

        direction = "long" if peak_price > start_price else "short"

        # find approximate end
        end_idx = peak_idx
        for j in range(peak_idx, min(peak_idx + 80, len(close))):
            if direction == "long":
                retraction = (peak_price - close[j]) / (peak_price - start_price) if peak_price != start_price else 0
                if retraction > 0.5:
                    end_idx = j
                    break
            else:
                retraction = (close[j] - peak_price) / (start_price - peak_price) if start_price != peak_price else 0
                if retraction > 0.5:
                    end_idx = j
                    break
        else:
            end_idx = min(peak_idx + 50, len(close) - 1)

        prelim_events.append({
            "start_idx": start_idx,
            "end_idx": end_idx,
            "peak_idx": peak_idx,
            "start_price": start_price,
            "peak_price": peak_price,
            "direction": direction,
            "methods": [h.method],
            "magnitudes": [h.magnitude],
        })

    # step 2: merge overlapping events (greedy)
    merged: list[dict] = [prelim_events[0]]

    for ev in prelim_events[1:]:
        last = merged[-1]
        # check overlap
        overlap_start = max(last["start_idx"], ev["start_idx"])
        overlap_end = min(last["end_idx"], ev["end_idx"])

        if overlap_start < overlap_end:
            # there is overlap — check if significant
            overlap_bars = overlap_end - overlap_start
            ev_range = ev["end_idx"] - ev["start_idx"]
            last_range = last["end_idx"] - last["start_idx"]
            min_range = min(overlap_bars, ev_range, last_range)

            if min_range > 0 and overlap_bars / min_range >= OVERLAP_THRESHOLD:
                # merge: extend range, combine methods
                new_end = max(last["end_idx"], ev["end_idx"])
                new_start = min(last["start_idx"], ev["start_idx"])
                # cap event size
                if new_end - new_start > MAX_EVENT_BARS:
                    merged.append(ev)
                    continue
                # keep the peak with larger magnitude
                if abs(ev["peak_price"] - ev["start_price"]) > abs(last["peak_price"] - last["start_price"]):
                    last["peak_idx"] = ev["peak_idx"]
                    last["peak_price"] = ev["peak_price"]
                    last["direction"] = ev["direction"]
                last["methods"] = list(set(last["methods"] + ev["methods"]))
                last["magnitudes"] = last["magnitudes"] + ev["magnitudes"]
                continue

        merged.append(ev)

    # step 3: build final event dicts
    events = []
    for ev in merged:
        events.append(_build_event_from_range(
            ev["start_idx"], ev["end_idx"], ev["peak_idx"],
            ev["methods"], close, ts, tf_ms, symbol, tf,
        ))

    return events


def _build_event(
    group: list[DetectionHit],
    close: np.ndarray,
    ts: np.ndarray,
    tf_ms: int,
    symbol: str,
    tf: str,
) -> dict:
    """Build a single event dict from a group of merged hits."""
    start_idx = group[0].bar_idx
    reasons = list({h.method for h in group})  # unique reasons

    # find peak: scan forward from start
    peak_idx = start_idx
    peak_price = close[start_idx]
    for j in range(start_idx, min(start_idx + MAX_EVENT_BARS, len(close))):
        if abs(close[j] - close[start_idx]) > abs(peak_price - close[start_idx]):
            peak_price = close[j]
            peak_idx = j

    direction = "long" if peak_price > close[start_idx] else "short"

    # find end: where price retraces significantly from peak
    end_idx = peak_idx
    for j in range(peak_idx, min(peak_idx + MAX_EVENT_BARS // 2, len(close))):
        if direction == "long":
            retraction = (peak_price - close[j]) / (peak_price - close[start_idx]) if peak_price != close[start_idx] else 0
            if retraction > 0.5:
                end_idx = j
                break
        else:
            retraction = (close[j] - peak_price) / (close[start_idx] - peak_price) if close[start_idx] != peak_price else 0
            if retraction > 0.5:
                end_idx = j
                break
    else:
        end_idx = min(peak_idx + MAX_EVENT_BARS // 4, len(close) - 1)

    start_price = float(close[start_idx])
    end_price = float(close[end_idx])
    magnitude = abs(peak_price - start_price) / start_price * 100

    # max drawdown before peak (for longs:最大回落 from peak)
    max_drawdown = 0.0
    if direction == "long":
        running_high = start_price
        for j in range(start_idx, peak_idx + 1):
            if close[j] > running_high:
                running_high = close[j]
            dd = (running_high - close[j]) / running_high * 100
            max_drawdown = max(max_drawdown, dd)
    else:
        running_low = start_price
        for j in range(start_idx, peak_idx + 1):
            if close[j] < running_low:
                running_low = close[j]
            dd = (close[j] - running_low) / running_low * 100
            max_drawdown = max(max_drawdown, dd)

    # max pullback after peak
    max_pullback = 0.0
    if direction == "long":
        for j in range(peak_idx, end_idx + 1):
            pb = (peak_price - close[j]) / peak_price * 100
            max_pullback = max(max_pullback, pb)
    else:
        for j in range(peak_idx, end_idx + 1):
            pb = (close[j] - peak_price) / peak_price * 100
            max_pullback = max(max_pullback, pb)

    return {
        "symbol": symbol,
        "timeframe": tf,
        "event_start_ts": int(ts[start_idx]),
        "event_peak_ts": int(ts[peak_idx]),
        "event_end_ts": int(ts[end_idx]),
        "direction": direction,
        "magnitude_pct": round(magnitude, 2),
        "duration_bars": end_idx - start_idx,
        "duration_ms": (end_idx - start_idx) * tf_ms,
        "start_price": start_price,
        "peak_price": float(peak_price),
        "end_price": end_price,
        "max_drawdown_before_peak_pct": round(max_drawdown, 2),
        "max_pullback_after_peak_pct": round(max_pullback, 2),
        "trigger_reasons": reasons,
        "n_triggers": len(reasons),
    }


def _build_event_from_range(
    start_idx: int,
    end_idx: int,
    peak_idx: int,
    methods: list[str],
    close: np.ndarray,
    ts: np.ndarray,
    tf_ms: int,
    symbol: str,
    tf: str,
) -> dict:
    """Build a single event dict from an explicit bar range."""
    start_price = float(close[start_idx])
    peak_price = float(close[peak_idx])
    end_price = float(close[end_idx])
    direction = "long" if peak_price > start_price else "short"
    magnitude = abs(peak_price - start_price) / start_price * 100

    # max drawdown before peak
    max_drawdown = 0.0
    if direction == "long":
        running_high = start_price
        for j in range(start_idx, peak_idx + 1):
            if close[j] > running_high:
                running_high = close[j]
            dd = (running_high - close[j]) / running_high * 100
            max_drawdown = max(max_drawdown, dd)
    else:
        running_low = start_price
        for j in range(start_idx, peak_idx + 1):
            if close[j] < running_low:
                running_low = close[j]
            dd = (close[j] - running_low) / running_low * 100
            max_drawdown = max(max_drawdown, dd)

    # max pullback after peak
    max_pullback = 0.0
    if direction == "long":
        for j in range(peak_idx, end_idx + 1):
            pb = (peak_price - close[j]) / peak_price * 100
            max_pullback = max(max_pullback, pb)
    else:
        for j in range(peak_idx, end_idx + 1):
            pb = (close[j] - peak_price) / peak_price * 100
            max_pullback = max(max_pullback, pb)

    return {
        "symbol": symbol,
        "timeframe": tf,
        "event_start_ts": int(ts[start_idx]),
        "event_peak_ts": int(ts[peak_idx]),
        "event_end_ts": int(ts[end_idx]),
        "direction": direction,
        "magnitude_pct": round(magnitude, 2),
        "duration_bars": end_idx - start_idx,
        "duration_ms": (end_idx - start_idx) * tf_ms,
        "start_price": start_price,
        "peak_price": peak_price,
        "end_price": end_price,
        "max_drawdown_before_peak_pct": round(max_drawdown, 2),
        "max_pullback_after_peak_pct": round(max_pullback, 2),
        "trigger_reasons": methods,
        "n_triggers": len(methods),
    }


def discover_events_in_df(df: pl.DataFrame, symbol: str, tf: str) -> list[dict]:
    """Run all detection methods on a single OHLCV DataFrame."""
    if len(df) < ROLLING_WINDOW * 2:
        return []

    close = df["close"].to_numpy().astype(np.float64)
    high = df["high"].to_numpy().astype(np.float64)
    low = df["low"].to_numpy().astype(np.float64)
    volume = df["volume"].to_numpy().astype(np.float64)
    ts = df["timestamp"].to_numpy().astype(np.int64)
    tf_ms = _tf_to_ms(tf)

    # compute ATR
    atr = compute_atr(high, low, close)

    # run all detectors
    all_hits: list[DetectionHit] = []
    all_hits.extend(detect_volatility_breakout(close, high, low, atr, MIN_MAGNITUDE_PCT, ATR_MULTIPLIER))
    all_hits.extend(detect_percentile_move(close, MIN_MAGNITUDE_PCT, PERCENTILE_THRESHOLD))
    all_hits.extend(detect_multi_candle_impulse(close, MULTI_CANDLE_BARS, MULTI_CANDLE_TOTAL_PCT))
    all_hits.extend(detect_directional_run(close, DIRECTIONAL_RUN_BARS))
    all_hits.extend(detect_volume_spike(volume, close, VOLUME_SPIKE_RATIO))

    # merge overlapping detections into events
    events = merge_hits_to_events(all_hits, close, ts, tf_ms, symbol, tf)
    return events


def _save_discovery_config() -> None:
    """Save exact config used for this discovery run."""
    config = f"""# Discovery config — auto-generated, do not edit
# Used by: discover_events.py

[thresholds]
min_magnitude_pct = {MIN_MAGNITUDE_PCT}
atr_multiplier = {ATR_MULTIPLIER}
percentile_threshold = {PERCENTILE_THRESHOLD}
multi_candle_bars = {MULTI_CANDLE_BARS}
multi_candle_total_pct = {MULTI_CANDLE_TOTAL_PCT}
directional_run_bars = {DIRECTIONAL_RUN_BARS}
volume_spike_ratio = {VOLUME_SPIKE_RATIO}
rolling_window = {ROLLING_WINDOW}

[merge]
merge_window_bars = 20
overlap_threshold = 0.30

[scope]
event_lookback_days = {EVENT_LOOKBACK_DAYS}

[symbols]
{chr(10).join(f'"{s}"' for s in SYMBOLS)}

[timeframes]
{chr(10).join(f'"{t}"' for t in TIMEFRAMES)}
"""
    out = report_path("discovery_config.toml")
    out.write_text(config)


def discover_all(
    symbols: list[str] | None = None,
    timeframes: list[str] | None = None,
    version: int | None = None,
) -> pl.DataFrame:
    """Run event discovery across all symbols and timeframes."""
    symbols = symbols or SYMBOLS
    timeframes = timeframes or TIMEFRAMES

    if version is None:
        version = get_active_version()

    # save discovery config for reproducibility
    _save_discovery_config()

    all_events: list[dict] = []
    event_id = 0
    method_counts: dict[str, int] = {}

    for sym in symbols:
        for tf in timeframes:
            path = cache_path(sym, tf, version)
            if not path.exists():
                continue

            df = pl.read_parquet(path)
            print(f"Discovering: {sym} {tf} ({len(df)} bars) ... ", end="", flush=True)

            events = discover_events_in_df(df, sym, tf)
            for ev in events:
                ev["event_id"] = event_id
                event_id += 1
                # count methods
                for reason in ev["trigger_reasons"]:
                    method_counts[reason] = method_counts.get(reason, 0) + 1

            all_events.extend(events)
            print(f"{len(events)} events")

    if not all_events:
        print("No events found.")
        return pl.DataFrame()

    events_df = pl.DataFrame(all_events)

    # convert trigger_reasons list to string for parquet storage
    events_df = events_df.with_columns(
        pl.col("trigger_reasons").list.join(", ").alias("trigger_reasons_str")
    ).drop("trigger_reasons").rename({"trigger_reasons_str": "trigger_reasons"})

    # scope to the actual manipulation window — older volatility on these
    # coins is unrelated to what's being studied here
    n_before_cutoff = len(events_df)
    cutoff_ms = int(time.time() * 1000) - EVENT_LOOKBACK_DAYS * 86_400_000
    events_df = events_df.filter(pl.col("event_start_ts") >= cutoff_ms)
    print(f"\nLookback filter (last {EVENT_LOOKBACK_DAYS}d): "
          f"{n_before_cutoff} -> {len(events_df)} events")

    # MIN_MAGNITUDE_PCT is threaded through detect_volatility_breakout and
    # detect_percentile_move only — multi_candle_impulse/directional_run/
    # volume_spike have their own independent (lower) floors, so raising the
    # constant alone doesn't scope every method. Filter on the actual computed
    # magnitude_pct directly instead, uniformly across all 5 methods.
    n_before_magnitude = len(events_df)
    events_df = events_df.filter(pl.col("magnitude_pct") >= MIN_MAGNITUDE_PCT)
    print(f"Magnitude floor (>= {MIN_MAGNITUDE_PCT}%): "
          f"{n_before_magnitude} -> {len(events_df)} events")

    out = report_path("events.parquet")
    events_df.write_parquet(out)

    print(f"\n{'='*60}")
    print(f"TOTAL: {len(events_df)} events")
    print(f"{'='*60}")

    # method effectiveness summary
    print("\nDETECTION METHOD FREQUENCY:")
    for method, count in sorted(method_counts.items(), key=lambda x: -x[1]):
        print(f"  {method:<25} {count:>5} events")

    # per symbol
    print("\nBY SYMBOL:")
    for row in events_df.group_by("symbol").agg(pl.len().alias("n")).sort("symbol").iter_rows(named=True):
        print(f"  {row['symbol']:<20} {row['n']:>5}")

    # per timeframe
    print("\nBY TIMEFRAME:")
    for row in events_df.group_by("timeframe").agg(pl.len().alias("n")).sort("timeframe").iter_rows(named=True):
        print(f"  {row['timeframe']:<6} {row['n']:>5}")

    # trigger reason co-occurrence
    print("\nMULTI-TRIGGER EVENTS:")
    multi = events_df.filter(pl.col("n_triggers") > 1)
    print(f"  {len(multi)} events triggered by 2+ methods")
    if len(multi) > 0:
        for row in multi.select("trigger_reasons").head(10).iter_rows(named=True):
            print(f"    {row['trigger_reasons']}")

    return events_df


if __name__ == "__main__":
    discover_all()
