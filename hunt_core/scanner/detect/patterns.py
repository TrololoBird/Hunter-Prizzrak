"""Pattern A (long) and Pattern B (short) — persistent, incremental state machine.

Redesign (see state.py docstring for the full rationale): each call advances
a symbol's tracked pattern by AT MOST one stage. A stage, once confirmed, is
frozen into the persisted state and never re-derived — later stages are
checked fresh against current data but can never retroactively rewrite an
earlier stage. This mirrors how the transcripts describe a trader actually
working: notice a setup forming, wait (across many real chart checks) for
the next confirming event, act only once the whole sequence has genuinely
unfolded in order.

Removed relative to the previous version (backtested against 15 confirmed
manipulations, not grounded in the source transcripts):
- ADX-based bull/bear/neutral regime gate
- Weighted-sum confidence scoring with partial-confirmation thresholds
- Separate "bokovik-1"/"bokovik-2" bookkeeping (one accumulation phase)
- LTF confirmation as an optional bonus rather than a required gate
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import polars as pl


def _relocate_idx_by_ts(df: pl.DataFrame, ts: float | None) -> int | None:
    """Current index of the bar whose open-time == ``ts``, or None if it rolled off.

    Pattern A freezes the impulse across scan cycles, but the meso OHLCV window is
    refetched each cycle as a fixed-length rolling window, so a stored integer
    index points one bar later every time a bar closes (SCAN-1). Re-locating by the
    bar's timestamp keeps the reference on the true impulse bar, honouring the
    state.py timestamp-freeze guarantee.
    """
    if ts is None or "ts" not in df.columns:
        return None
    matches = (df["ts"] == float(ts)).arg_true()
    return int(matches[0]) if len(matches) else None


def _to_float(val: Any) -> float:
    return float(val) if val is not None else 0.0


from hunt_core.scanner.detect.events import (
    ohlcv_to_df, compute_features, atr,
    detect_impulse, detect_consecutive_impulse,
    detect_absorption,
    detect_bokovik, detect_sweep_low, detect_sweep_high,
    candle_fade_ratio, rejection_at_peak,
    two_bar_reversal,
    bos_up, bos_down, choch_bull, choch_bear,
    bullish_volume,
    is_ascending_channel, is_descending_channel,
    _adaptive_buffer,
)
from hunt_core.scanner.detect.state import Direction, PatternType, new_symbol_state, is_stale
from hunt_core.scanner.detect.scoring import full_confirmation_score

_MACRO_TF = "1d"
_MESO_TF_PRIORITY = ("4h", "1h")
_MACRO_LOOKBACK_BARS = 180
_MACRO_EXCLUDE_RECENT = 7
_MESO_RECENT_CANDIDATES = 12
_BOKOVIK_WINDOW = 30

# Manipulations form at different scales — the trader's own charts ran on 4h for
# the big ones (ESPORTS/ZEREBRO/BSB) but the same pump/dump geometry appears one
# and two TFs down on faster coins. A single hardcoded 4h meso can only ever see
# 4h-scale setups; research found real events topping out at 1h and 15m too.
# Each ladder = (macro context, meso detection, micro entry-confirm). The scanner
# runs every ladder per symbol, each with its own persisted state, so a setup at
# any scale is caught by its matching ladder.
_TF_LADDERS: tuple[tuple[str, str, str], ...] = (
    ("1w", "1d", "4h"),
    ("1d", "4h", "15m"),
    ("4h", "1h", "15m"),
    ("1h", "15m", "5m"),
)

# Macro context needs enough bars to hold a real extreme, but _MACRO_LOOKBACK_BARS//2
# (90 bars) means 90 WEEKS on a 1w frame — nearly two years of listing history.
# Measured on a 25-symbol watchlist sample: 10 symbols clear 90 weeks, 2 more sit in
# 40-89 (PAXG, MORPHO) and would be excluded for no structural reason, and 13 recent
# listings clear neither. 40 weeks (~9 months) is enough weekly context to place a
# macro extreme and stops the guard from being about listing age.
_MACRO_MIN_BARS: dict[str, int] = {"1w": 40}
_MACRO_MIN_BARS_DEFAULT = _MACRO_LOOKBACK_BARS // 2


# NB: stays a dataclass, NOT Pydantic (project rule), on purpose. The state machine
# builds it and then AMENDS it in place — setup.score / steps_covered / macro_tf /
# evidence are assigned after construction as later stages confirm. A Pydantic model
# would force a choice with no good side: frozen forbids the mutation; validate_assignment
# re-validates on every write and changes semantics; mutable-without-validation gains
# nothing over a dataclass. Converting it means restructuring build-then-amend in the
# scanner core — a real refactor with regression risk, not a mechanical swap. (The
# non-mutated value/config models — PrescanHit, HuntCandidate, UniverseConfig — ARE
# Pydantic; only the mutated ones stay dataclasses.)
@dataclass
class ManipulationSetup:
    direction: Direction
    pattern_type: PatternType
    score: float
    macro_tf: str = _MACRO_TF
    meso_tf: str = ""
    micro_tf: str | None = None
    micro_confirmed: bool = True
    swept_level: float = 0.0
    sweep_extreme: float = 0.0
    target: float | None = None
    target_ladder: tuple[float, ...] = ()  # full structural TP ladder (course: пулы ликвидности)
    entry_ref: float | None = None
    evidence: tuple[str, ...] = ()
    steps_covered: int = 0
    total_steps: int = 0
    bokovik_count: int = 1
    funding_note: str = ""  # human funding read (perp crowding / слив-timing), if any


# Funding thresholds (Binance per-8h rate, decimal). Elevated positive funding =
# crowded longs = squeeze-short fuel (real event: EVAA peaked +0.108%). Funding
# rolling over from a hot peak = distribution done → "основной слив" timing.
# Extreme negative = crowded shorts (real event: THE cratered to -1.43%) = squeeze
# risk on a FRESH short. Non-gating: absent/flat funding never blocks a structural
# short (THE had ~0 funding at entry yet dumped) — funding only ADDS conviction.
_FUND_ELEVATED = 0.0003
_FUND_HOT = 0.0006
_FUND_SQUEEZE_RISK = -0.003


def _funding_short_signal(fctx: dict[str, Any] | None) -> tuple[list[str], float, str]:
    """(evidence_tokens, score_bump, human_note) for a SHORT given funding context.

    ``fctx`` = {"rate": last 8h funding, "peak": max over recent window}. This is
    the missing discriminator between a crowded-long squeeze (short works, EVAA)
    and a genuine continuation pump with no crowding (short gets run over) — see
    the modeled EVAA/THE analysis and the MUSDT Pattern-C false positives."""
    if not fctx:
        return [], 0.0, ""
    rate = float(fctx.get("rate") or 0.0)
    peak = float(fctx.get("peak") or 0.0)
    ev: list[str] = []
    bump = 0.0
    notes: list[str] = []
    rolling_over = peak >= _FUND_ELEVATED and rate <= peak * 0.6
    if peak >= _FUND_HOT:
        ev.append("funding_hot")
        bump += 0.15
        notes.append(f"фандинг перегрет (пик {peak * 100:+.3f}%, лонги в толпе)")
    elif peak >= _FUND_ELEVATED:
        ev.append("funding_elevated")
        bump += 0.08
        notes.append(f"фандинг повышен (пик {peak * 100:+.3f}%)")
    if rolling_over:
        ev.append("funding_rollover")
        bump += 0.10
        notes.append(f"фандинг откатывается {peak * 100:+.3f}%→{rate * 100:+.3f}% — основной слив")
    if rate <= _FUND_SQUEEZE_RISK:
        ev.append("funding_squeeze_risk")
        notes.append(f"фандинг {rate * 100:+.2f}% (толпа шортов — риск сквиза, не наращивать)")
    return ev, bump, "; ".join(notes)


def _macro_extreme(df: pl.DataFrame, *, direction: Direction) -> float | None:
    exclude = _MACRO_EXCLUDE_RECENT
    body = df[:-exclude] if exclude < len(df) else df
    window = body.tail(_MACRO_LOOKBACK_BARS)
    if window.height == 0:
        return None
    if direction == "short":
        return _to_float(window["high"].max())
    return _to_float(window["low"].min())


def _prior_swing_high(df: pl.DataFrame, lookback: int = 60, *, exclude_last: int = 15) -> tuple[float, int] | None:
    """Most recent swing high BEFORE the pump (exclude last N bars = the impulse itself).

    Returns (price, bar_index_into_df) or None.
    """
    start = max(0, len(df) - lookback)
    body = df.slice(start, lookback)
    if exclude_last > 0 and len(body) > exclude_last:
        body = body[:-exclude_last]
    if len(body) < 10:
        return None
    df_c = compute_features(body)
    sw = df_c.filter(pl.col("_swing_high"))
    if sw.is_empty():
        return None
    last_sh = float(sw["high"].tail(1)[0])
    # Scan body from the end to find the bar index within body
    for i in range(len(body) - 1, -1, -1):
        if abs(float(body["high"][i]) - last_sh) / max(last_sh, 1e-9) < 0.001:
            return last_sh, start + i
    return last_sh, start


def _micro_df(micro_15m: pl.DataFrame | None, meso_df: pl.DataFrame) -> pl.DataFrame:
    return micro_15m if micro_15m is not None and len(micro_15m) > 20 else meso_df.tail(50)


def _consolidation_long_entry(meso_df: pl.DataFrame, bokovik: dict[str, Any]) -> float:
    """Best long entry = near the BOTTOM of the accumulation (course video: the
    trader enters "у низа второй консолидации" for the best price, not at the
    break). Anchor to the volume POC of the consolidation window when it sits in
    the lower half of the range; otherwise the range low. Reuses the project's
    own fixed-range volume profile (same primitive Prizrak's poc.py uses) — no
    reimplementation of the bucket math.
    """
    lo = float(bokovik.get("lo") or 0.0)
    hi = float(bokovik.get("hi") or 0.0)
    if lo <= 0 or hi <= lo:
        return float(meso_df["close"][-1])
    mid = (lo + hi) / 2.0
    try:
        from hunt_core.features.volume_profile import volume_profile_levels
        window = meso_df.tail(_BOKOVIK_WINDOW).select(["high", "low", "volume"])
        poc, _vah, _val = volume_profile_levels(window, buckets=40, value_area_pct=0.70)
    except Exception:
        poc = None
    # prefer the POC only when it's in the lower half (a genuine accumulation
    # floor); a POC up near resistance is not where the trader buys.
    if poc is not None and lo <= poc <= mid:
        return float(poc)
    return lo


def _count_touches(df: pl.DataFrame, level: float, tolerance_pct: float = 0.005) -> int:
    """Count bars that touched a price level within tolerance."""
    lo = level * (1.0 - tolerance_pct)
    hi = level * (1.0 + tolerance_pct)
    return int(((df["high"] >= lo) & (df["low"] <= hi)).sum())


def _poc_level(df: pl.DataFrame) -> float | None:
    """Compute POC level from OHLCV data. Returns None if insufficient data."""
    if df is None or df.height < 10:
        return None
    try:
        from hunt_core.features.volume_profile import volume_profile_levels
        poc, _vah, _val = volume_profile_levels(
            df.select(["high", "low", "volume"]),
            buckets=20, value_area_pct=0.70,
        )
        return poc
    except Exception:
        return None


def _funding_target_mult(direction: Direction, fctx: dict[str, Any] | None) -> float:
    """Target-distance multiplier from funding context.

    SHORT + elevated positive funding (crowded longs → squeeze fuel) → up to 1.3×.
    LONG + elevated negative funding (crowded shorts → short squeeze) → up to 1.3×.
    """
    if not fctx:
        return 1.0
    rate = float(fctx.get("rate") or 0.0)
    if direction == "short" and rate > _FUND_ELEVATED:
        return min(1.0 + (rate / _FUND_HOT) * 0.3, 1.3)
    if direction == "long" and rate < -_FUND_ELEVATED:
        return min(1.0 + (abs(rate) / _FUND_HOT) * 0.3, 1.3)
    return 1.0


def _target_ladder(
    macro_df: pl.DataFrame, meso_df: pl.DataFrame, *,
    entry: float, direction: Direction,
    htf_df: pl.DataFrame | None = None,
    funding_mult: float = 1.0,
) -> list[float]:
    """Structural target ladder from HTF swing levels + POC zones.

    Collects swing highs/lows from all TFs, plus POC levels from HTF/meso.
    Scores each level by significance: POC > multi-tested swing > single swing.
    ``funding_mult`` amplifies distance scores when funding confirms the direction.
    Returns only levels ≥ 20 % from entry, nearest-first, deduped within 1.5 %.
    Returns at most 3 targets. Empty list when no structural level reaches 20 %.
    """
    frames = [htf_df, meso_df, macro_df] if htf_df is not None else [meso_df, macro_df]

    # ── Collect candidate levels ──
    raw_candidates: list[float] = []
    for df in frames:
        if df is None or df.height < 12:
            continue
        # Swing levels
        feat = compute_features(df)
        col = "low" if direction == "short" else "high"
        swing_col = "_swing_low" if direction == "short" else "_swing_high"
        for v in feat.filter(pl.col(swing_col))[col]:
            fv = float(v)
            if direction == "short" and fv < entry:
                raw_candidates.append(fv)
            elif direction == "long" and fv > entry:
                raw_candidates.append(fv)

    if not raw_candidates:
        return []

    # ── Score each level ──
    scored: dict[float, float] = {}
    for lv in set(raw_candidates):
        pct = ((lv - entry) / entry) if direction == "long" else ((entry - lv) / entry)
        if pct < 0.20:
            continue  # too close for a structural target
        # Count touches across all available TFs (multi-tested = higher significance)
        total_touches = 0
        for df in frames:
            if df is not None and df.height >= 10:
                total_touches += _count_touches(df, lv)
        # Base score = distance pct (farther = better), boosted by touches + funding
        score = pct * (1.0 + min(total_touches, 10) * 0.15) * funding_mult
        scored[lv] = score

    if not scored:
        return []

    # ── Add POC levels as high-priority candidates ──
    for df in frames[:2]:  # HTF and meso only
        poc = _poc_level(df)
        if poc is not None:
            pct = ((poc - entry) / entry) if direction == "long" else ((entry - poc) / entry)
            if pct >= 0.20 and poc not in scored:
                total_touches = 0
                for d in frames:
                    if d is not None and d.height >= 10:
                        total_touches += _count_touches(d, poc)
                # POC gets a bonus multiplier, funding amplifies further
                scored[poc] = pct * (1.5 + min(total_touches, 10) * 0.15) * funding_mult

    # ── Select top 3 by score, nearest-first among equals ──
    ranked = sorted(scored.items(), key=lambda x: (-x[1], abs(x[0] - entry)))

    # Dedupe within 1.5 %
    out: list[float] = []
    for lv, _score in ranked:
        if not out or abs(lv - out[-1]) / max(abs(out[-1]), 1e-9) >= 0.015:
            out.append(lv)
        if len(out) >= 3:
            break

    # Sort nearest-first
    out.sort(reverse=(direction == "short"))
    return out


def _measured_move_target(meso_df: pl.DataFrame, entry: float, *, lookback: int = 60) -> float:
    """Reachable long target = entry + the manipulation amplitude (recent high−low).

    Pattern C used ``_target_ladder[0]`` — the ≥20 %-floored, farther-biased distance
    pool — which manufactured fantasy-RR / timeouts (full dataset_v11 replay: C avgR
    −0.484, 46 timeouts / 202). Projecting the recent manipulation amplitude up from
    entry gives the reachable structural magnitude the move actually banks, mirroring
    Pattern B's impulse-low fix. Validated on full dataset_v11 (research/backtest_c_
    target.py): flips C −0.484 → +0.127, losses 102→67, timeouts 46→28.
    """
    if entry <= 0 or meso_df.height < 2:
        return 0.0
    seg = meso_df.tail(min(lookback, meso_df.height))
    amp = _to_float(seg["high"].max()) - _to_float(seg["low"].min())
    return entry + amp if amp > 0 else 0.0


def _htf_trend_bias(df: pl.DataFrame | None) -> str | None:
    """Determine trend bias from HTF channel using 20/50 EMA position + slope.

    Returns 'bull', 'bear', or None (neutral / insufficient data).
    """
    if df is None or df.height < 60:
        return None
    close = df["close"]
    ema20 = close.rolling_mean(20)
    ema50 = close.rolling_mean(50)
    last_close = float(close.tail(1)[0])
    last_ema20 = float(ema20.tail(1)[0])
    last_ema50 = float(ema50.tail(1)[0])
    if last_close <= 0 or last_ema20 <= 0 or last_ema50 <= 0:
        return None
    above_20 = last_close > last_ema20 * 1.01
    above_50 = last_close > last_ema50 * 1.01
    ema20_vals = ema20.tail(5).to_list()
    slope_up = len(ema20_vals) >= 2 and ema20_vals[-1] > ema20_vals[0]
    slope_down = len(ema20_vals) >= 2 and ema20_vals[-1] < ema20_vals[0]
    if above_20 and above_50 and slope_up:
        return "bull"
    if not above_20 and not above_50 and slope_down:
        return "bear"
    return None


def _expected_move_pct(
    direction: Direction, entry: float, extreme: float,
    bokovik: dict[str, Any] | None = None,
) -> float:
    """Expected clean price move % from the manipulation structure.

    For LONG: bokovik width × 2 (conservative), or sweep distance × 3, whichever larger.
    For SHORT: sweep distance × 1.5.
    Returns 0.0 if insufficient data (no bokovik, no meaningful sweep).
    """
    expected = 0.0
    if direction == "long":
        if bokovik is not None:
            bw = (bokovik.get("hi", 0) - bokovik.get("lo", 0)) / max(entry, 1e-9)
            expected = max(expected, bw * 2.0)
        sweep_pct = abs(entry - extreme) / max(entry, 1e-9)
        expected = max(expected, sweep_pct * 3.0)
    else:
        sweep_pct = abs(extreme - entry) / max(entry, 1e-9)
        expected = max(expected, sweep_pct * 1.5)

    return min(expected, 0.80)


def _build_setup(
    *, pattern_type: PatternType, direction: Direction, meso_tf: str,
    swept_level: float, sweep_extreme: float, target: float | None,
    entry_ref: float | None, evidence: list[str], total_steps: int,
    target_ladder: tuple[float, ...] = (),
    micro_confirmed: bool = False,
    micro_tf: str = "15m",
) -> ManipulationSetup:
    return ManipulationSetup(
        direction=direction,
        pattern_type=pattern_type,
        score=full_confirmation_score(),
        meso_tf=meso_tf,
        micro_tf=micro_tf,
        micro_confirmed=micro_confirmed,
        swept_level=swept_level,
        sweep_extreme=sweep_extreme,
        target=target,
        target_ladder=tuple(target_ladder),
        entry_ref=entry_ref,
        evidence=tuple(evidence),
        steps_covered=total_steps,
        total_steps=total_steps,
    )


# ── Pattern A / Type 1 (long): dump impulse -> absorption -> bokovik -> sweep -> break 100-400%
#    Pattern A3 / Type 3 (long, no impulse): descending channel -> bokovik -> break 100%+
_A_TOTAL_STEPS = 5
_A3_TOTAL_STEPS = 2


def _advance_pattern_a(
    macro_df: pl.DataFrame, meso_df: pl.DataFrame, meso_tf: str,
    micro_15m: pl.DataFrame | None, state: dict[str, Any], now_ms: float,
    *,
    micro_tf: str = "15m",
    htf_df: pl.DataFrame | None = None,
    htf_bias: str | None = None,
    funding_ctx: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], ManipulationSetup | None]:
    stage = int(state.get("stage", 0))
    pattern = state.get("pattern")
    data: dict[str, Any] = dict(state.get("data") or {})

    if stage == 0:
        # Method (MANIPULATION_METHODOLOGY §2 / Type 1): the opening leg is an
        # AGGRESSIVE PUMP UP, then a one-candle ABSORPTION back down, then bokovik →
        # sweep down → bokovik-2 → BOS/CHoCH up. The impulse to seed on is therefore
        # the UP pump — detecting a DOWN impulse (the old code) treated the absorption
        # dump as the impulse and inverted the whole formation (a falling-knife
        # V-recovery long, not the engineered pump→absorb→accumulate setup). detect_
        # absorption is direction-agnostic: on an UP impulse it correctly measures the
        # down retrace (the "поглощение").
        imp_ok, imp_idx = detect_impulse(meso_df, lookback=30, direction="up")
        if not imp_ok:
            imp_ok, imp_idx = detect_consecutive_impulse(meso_df, min_count=3, direction="up")
        if imp_ok and imp_idx is not None:
            return {
                "pattern": "A", "stage": 1, "anchor_ts": now_ms, "first_ts": now_ms,
                "meso_tf": meso_tf,
                "data": {
                    "impulse_idx": int(imp_idx),
                    # Freeze the impulse bar's OPEN-TIME so stage 1 can re-locate it
                    # after the rolling meso window shifts the integer index (SCAN-1).
                    "impulse_ts": float(meso_df["ts"][imp_idx]),
                },
            }, None
        b1 = detect_bokovik(meso_df, window=_BOKOVIK_WINDOW)
        # Type 3 (author's descending-channel long): persistent downtrend
        # (lower highs+lows) → bokovik near channel bottom → break 100%+
        if b1 is not None and is_descending_channel(meso_df, lookback=min(50, len(meso_df)), min_swings=2):
            return {
                "pattern": "A3", "stage": 1, "anchor_ts": now_ms, "first_ts": now_ms,
                "meso_tf": meso_tf, "data": {"bokovik": b1},
            }, None
        return new_symbol_state(), None

    if pattern == "A":
        if stage == 1:
            # Re-locate the impulse bar by its frozen open-time so the rolling meso
            # window doesn't drift the reference (SCAN-1). Fall back to the stored
            # index only for legacy state rows that predate impulse_ts; if the ts
            # rolled off the window, imp_idx is None → wait (staleness reset clears).
            imp_ts = data.get("impulse_ts")
            if imp_ts is not None:
                imp_idx = _relocate_idx_by_ts(meso_df, imp_ts)
            else:
                imp_idx = data.get("impulse_idx")
            if imp_idx is None or imp_idx >= len(meso_df) or not detect_absorption(meso_df, imp_idx):
                return state, None
            data["absorption_confirmed"] = True
            return {**state, "stage": 2, "anchor_ts": now_ms, "data": data}, None

        if stage == 2:
            b1 = detect_bokovik(meso_df, window=_BOKOVIK_WINDOW)
            if b1 is None:
                return state, None
            data["bokovik"] = b1
            return {**state, "stage": 3, "anchor_ts": now_ms, "data": data}, None

        if stage == 3:
            # The floor low was swept — now ARM and wait for the LTF слом that
            # confirms the reversal. Emitting at the bare sweep (the old behaviour)
            # is a falling knife: backtest (dataset_v8+v9) showed EVERY Type-1 floor
            # entry was ltf_pending (by construction the 15m up-break cannot exist on
            # the same bar the low is swept) and went 0W/25L/18timeout. The author's
            # own reliable Type-1 entry is «когда у нас будет подтверждение слома
            # структуры нисходящей» — so confirmation is a gate, taken on stage 4.
            bokovik = data.get("bokovik")
            if not bokovik:
                return new_symbol_state(), None
            # Constrain the sweep scan to the consolidation window: the bokovik low
            # is the min of the last _BOKOVIK_WINDOW bars, so a sweep of it must be
            # recent — scanning the whole frame let an old pierce satisfy the gate.
            sweep_ok, sweep_extreme, _ = detect_sweep_low(
                meso_df.tail(_BOKOVIK_WINDOW), bokovik["lo"]
            )
            if not sweep_ok:
                return state, None
            data["sweep_extreme"] = float(sweep_extreme)
            return {**state, "stage": 4, "anchor_ts": now_ms, "data": data}, None

        if stage == 4:
            # Await the LTF слом confirmation before entering the floor long.
            bokovik = data.get("bokovik")
            if not bokovik:
                return new_symbol_state(), None
            sweep_extreme = float(data.get("sweep_extreme") or bokovik.get("lo") or 0.0)
            swept_low = float(bokovik.get("lo") or 0.0)
            # Floor decisively lost — the долгий thesis is dead, re-arm from scratch.
            if swept_low > 0 and float(meso_df["close"][-1]) < swept_low * 0.95:
                return new_symbol_state(), None
            micro_df = _micro_df(micro_15m, meso_df)
            if not (bos_up(micro_df) or choch_bull(micro_df)):
                return state, None  # no слом yet — keep waiting
            vol_ok = bullish_volume(meso_df) or (micro_15m is not None and bullish_volume(micro_15m))
            a_entry = _consolidation_long_entry(meso_df, bokovik)
            a_ladder = _target_ladder(macro_df, meso_df, entry=a_entry, direction="long", htf_df=htf_df,
                                      funding_mult=_funding_target_mult("long", funding_ctx))
            evidence = ["impulse", "absorption", "bokovik", "sweep_below", "ltf_confirmed"]
            if not vol_ok:
                evidence.append("volume_pending")
            if htf_bias:
                evidence.append(f"htf_{htf_bias}")
            setup = _build_setup(
                pattern_type="A", direction="long", meso_tf=meso_tf,
                swept_level=swept_low,
                sweep_extreme=sweep_extreme,
                target=(a_ladder[0] if a_ladder else _to_float(meso_df["high"].max())),
                target_ladder=tuple(a_ladder),
                entry_ref=a_entry,
                evidence=evidence,
                total_steps=_A_TOTAL_STEPS,
                micro_confirmed=True,
                micro_tf=micro_tf,
            )
            setup.score = 1.0
            setup.steps_covered = _A_TOTAL_STEPS
            setup.macro_tf = meso_tf
            if not a_ladder:
                exp = _expected_move_pct("long", a_entry, sweep_extreme, bokovik=bokovik)
                if exp < 0.20:
                    return new_symbol_state(), None
            return new_symbol_state(), setup

    if pattern == "A3" and stage == 1:
        # A3 (accumulation, no prior impulse): ARM at the accumulation FLOOR (price in
        # the lower half of the боковик), then wait for the LTF слом on stage 2 before
        # entering. Emitting on the bare floor was a falling knife — backtest showed
        # A3 floor entries were 100% ltf_pending and went 0W/19L/12timeout. The
        # author's reliable floor long needs the reversal confirmation, not just a
        # price sitting low in the range.
        bokovik = data.get("bokovik") or {}
        lo = float(bokovik.get("lo") or 0.0)
        hi = float(bokovik.get("hi") or 0.0)
        if lo <= 0 or hi <= lo:
            return new_symbol_state(), None
        cur = float(meso_df["close"][-1])
        if cur > (lo + hi) / 2.0:
            return state, None  # price mid/upper range — wait for a retest of the floor
        return {**state, "stage": 2, "anchor_ts": now_ms, "data": data}, None

    if pattern == "A3" and stage == 2:
        bokovik = data.get("bokovik") or {}
        lo = float(bokovik.get("lo") or 0.0)
        hi = float(bokovik.get("hi") or 0.0)
        if lo <= 0 or hi <= lo:
            return new_symbol_state(), None
        # Accumulation floor lost — reset.
        if float(meso_df["close"][-1]) < lo * 0.95:
            return new_symbol_state(), None
        micro_df = _micro_df(micro_15m, meso_df)
        if not (bos_up(micro_df) or choch_bull(micro_df)):
            return state, None  # no слом yet — keep waiting
        ltf_confirmed = True
        vol_ok = bullish_volume(meso_df) or (micro_15m is not None and bullish_volume(micro_15m))
        a3_entry = _consolidation_long_entry(meso_df, bokovik)
        # A3 has no sweep — anchor sweep_extreme one full ATR below the bokovik
        # low to create structural breathing room for the stop. Without this the
        # stop sits at lo*(1-buf), just the buffer below entry — too tight for
        # crypto noise on an accumulation-floor entry.
        _a3_atr = atr(meso_df.tail(_BOKOVIK_WINDOW * 2), 14)
        sweep_extreme = (lo - _a3_atr) if _a3_atr > 0 and lo - _a3_atr > 0 else lo * 0.97
        a3_ladder = _target_ladder(macro_df, meso_df, entry=a3_entry, direction="long", htf_df=htf_df,
                                   funding_mult=_funding_target_mult("long", funding_ctx))
        evidence = ["accumulation_no_impulse", "ltf_confirmed" if ltf_confirmed else "ltf_pending"]
        if not vol_ok:
            evidence.append("volume_pending")
        if htf_bias:
            evidence.append(f"htf_{htf_bias}")
        setup = _build_setup(
            pattern_type="A3", direction="long", meso_tf=meso_tf,
            swept_level=lo,
            sweep_extreme=sweep_extreme,
            target=(a3_ladder[0] if a3_ladder else _to_float(meso_df["high"].max())),
            target_ladder=tuple(a3_ladder),
            entry_ref=a3_entry,
            evidence=evidence,
            total_steps=_A3_TOTAL_STEPS,
            micro_confirmed=ltf_confirmed,
            micro_tf=micro_tf,
        )
        base_score = 1.0 if ltf_confirmed else 0.7
        setup.score = base_score  # A3: no volume penalty (quiet accumulation)
        setup.steps_covered = _A3_TOTAL_STEPS if ltf_confirmed else _A3_TOTAL_STEPS - 1
        setup.macro_tf = meso_tf
        # Skip if no structural target ≥ 20 % from entry
        if not a3_ladder:
            exp = _expected_move_pct("long", a3_entry, sweep_extreme, bokovik=bokovik)
            if exp < 0.20:
                return new_symbol_state(), None
        return new_symbol_state(), setup

    return new_symbol_state(), None


# ── Pattern B (short): sweep of a prior high -> fade/rejection -> LTF break
_B_TOTAL_STEPS = 3
# Source-faithful selectivity (research/manipulations_corpus/, MANTA разбор):
# the clean short is a "жирный" pump — the author explicitly dismisses 20–35% moves
# ("мелочи … любим движение пожирнее") and skips no-formation pumps ("не ловить фому").
# The pump amplitude (base→high) must clear this floor, else it is not the trade.
# Backtest (dataset_v10): without this gate Pattern B fired 175 setups at 46% loss;
# the deepest-pool target also manufactured 57 timeouts. The impulse-low target +
# this magnitude gate cut timeouts 57→14 and setups to the hand-picked few the
# method actually takes.
_B_MIN_PUMP_PCT = 0.40  # pump base→high must be ≥ 40% (a real manipulation, not chop)
# The take-profit is the IMPULSE-SET LOW = full absorption of the pump ("тяните сделку
# на полное поглощение пампа … тейк чуть ниже лоя, поставленного импульсом — там всегда
# сидит продавец / основные объёмы"). We target just inside that low, not a distant pool.
_B_TP_INSIDE_FRAC = 0.02  # TP placed 2% above the pump-base low (course: "чуть ниже него")
# The recent-peak window: the span that DEFINES both the "did the trend peak here"
# context gate and `pump_high`, the peak stage 1's fade/rejection detectors anchor on.
# Stage 0's sweep scan is bounded by it too — mirroring the A-side fix (`detect_sweep_low(
# meso_df.tail(_BOKOVIK_WINDOW), …)`: "scanning the whole frame let an old pierce satisfy
# the gate"). A is bound to 30 because that is the window detect_bokovik derives its low
# from; B is bound to 20 because that is the window it derives pump_high from — each
# window is tied to the definition of the level being swept, not copied across patterns.
# Without this, a coin that swept its macro high months ago and has since drifted back
# within 8% of it seeds stage 1 off the ANCIENT wick (the dist_pct/meso_top gates only
# test the CURRENT price), and the emitted "sweep→fade" is two causally unrelated events.
_B_PEAK_WINDOW = 20


def _advance_pattern_b(
    macro_df: pl.DataFrame, meso_df: pl.DataFrame, meso_tf: str,
    micro_15m: pl.DataFrame | None, state: dict[str, Any], now_ms: float,
    macro_tf: str = _MACRO_TF,
    *,
    micro_tf: str = "15m",
    htf_df: pl.DataFrame | None = None,
    htf_bias: str | None = None,
    funding_ctx: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], ManipulationSetup | None]:
    stage = int(state.get("stage", 0))
    data: dict[str, Any] = dict(state.get("data") or {})

    if stage == 0:
        # Sweep target must be a genuine TREND high (the macro extreme), not any
        # interior local swing. Transcript (GTC/Pattern B): the short is the
        # "финальный импульс на снятие ликвидности — обновление максимумов" of an
        # uptrend, then reversal. Firing on a lower local high during the pre-pump
        # accumulation chop is exactly the false short that shook out on real long
        # pumps (EVAA/LAB) — so the sweep must reach at/near the macro high, and
        # the meso window's own top must actually be that high (an uptrend led
        # here), not an interior peak.
        current_price = float(meso_df["close"][-1])
        macro_high = _macro_extreme(macro_df, direction="short")
        if macro_high is None or macro_high <= 0:
            return new_symbol_state(), None
        dist_pct = (macro_high - current_price) / macro_high * 100.0
        if dist_pct > 8.0:
            return new_symbol_state(), None  # price nowhere near a trend high — not a Pattern B context
        # the recent meso top must be at/above the macro high (the trend really
        # peaked here), not a pullback high far below it
        meso_top = _to_float(meso_df["high"].tail(_B_PEAK_WINDOW).max())
        if meso_top < macro_high * 0.98:
            return new_symbol_state(), None
        sweep_target = macro_high

        # Bound the sweep scan to the same recent window that defines pump_high, so
        # the sweep and the peak stage 1 fades are the SAME event (see _B_PEAK_WINDOW).
        sweep_ok, sweep_extreme, _ = detect_sweep_high(
            meso_df.tail(_B_PEAK_WINDOW), sweep_target
        )
        if not sweep_ok:
            return new_symbol_state(), None
        pump_high = _to_float(meso_df["high"].tail(_B_PEAK_WINDOW).max())
        return {
            "pattern": "B", "stage": 1, "anchor_ts": now_ms, "first_ts": now_ms,
            "meso_tf": meso_tf,
            "data": {"swept_level": sweep_target, "sweep_extreme": sweep_extreme, "pump_high": pump_high},
        }, None

    if stage == 1:
        # Variant A (course-faithful, predictive): the short is taken AT THE PEAK
        # the moment the swept high is faded/rejected ("это уже сформированный хай…
        # свеча закрылась красной"), NOT after the 15m breaks down. Waiting for
        # bos_down/choch_bear on the micro made the entry late — the dump was
        # already running. So emit here on sweep+fade; the LTF break is only a
        # STRENGTH upgrade (ltf_confirmed vs ltf_pending), not an emission gate.
        # The stop sits above the sweep high with a buffer and the delivery carries
        # an averaging zone, so a further squeeze past the high is handled by
        # scaling in (усреднение) — exactly the trader's own risk model.
        ph = data.get("pump_high")
        if ph is None:
            return new_symbol_state(), None
        body_ratio, range_ratio = candle_fade_ratio(meso_df, n=8, peak_high=ph)
        fade_ok = body_ratio <= 0.50 and range_ratio <= 0.60
        reject_ok = rejection_at_peak(meso_df, ph)
        two_bar_ok = two_bar_reversal(meso_df, ph)
        if not (fade_ok or reject_ok or two_bar_ok):
            return state, None
        fade_kind = "candle_fade" if fade_ok else ("instant_rejection" if reject_ok else "two_bar_reversal")
        pump_high_f = float(ph)
        # Pump base = the impulse-set low the pump launched from ("лой, поставленный
        # импульсом"). This is BOTH the take-profit (full absorption) AND the amplitude
        # reference for selectivity. Use bar lows (not closes) over the pump window so
        # the base is the true structural low a wick set, not a conservative close.
        pump_window = meso_df.tail(min(60, len(meso_df)))
        pump_low = _to_float(pump_window["low"].min())
        pump_range = pump_high_f - pump_low
        # ── Source-faithful gate 1: SELECTIVITY (magnitude). Skip small pumps —
        # the method takes only "жирные" moves, dismisses 20–35% "мелочи". ──
        pump_amp = pump_range / pump_high_f if pump_high_f > 0 else 0.0
        if pump_amp < _B_MIN_PUMP_PCT:
            return state, None
        # ── Source-faithful gate 2: ABSORPTION. The clean signal is "предыдущий
        # максимум обновили ИМПУЛЬСОМ и цену СРАЗУ поглотили" — a genuine up-impulse
        # made the high AND price has since retraced (absorbed) it. A bare fade of a
        # non-impulse high is the noisy short that gets stopped (dataset_v10: 46% B
        # loss rate). detect_impulse(up) + detect_absorption is the exact primitive
        # Pattern A already uses on the long side; wire it into B for symmetry. ──
        imp_ok, imp_idx = detect_impulse(meso_df, lookback=30, direction="up")
        absorbed = bool(imp_ok and imp_idx is not None and detect_absorption(meso_df, imp_idx))
        if not absorbed:
            return state, None
        micro_df = _micro_df(micro_15m, meso_df)
        ltf_confirmed = bool(bos_down(micro_df) or choch_bear(micro_df))
        # TP = just inside the impulse-set low (full pump absorption), NOT a distant
        # distance-ranked pool. This is the reachable level the method banks at; the
        # deepest structural pool only inflated RR into fantasy (ZEC #18718: TP 220.5,
        # −57%, RR 14.43, closed −3%) and manufactured timeouts. Keep any deeper pool
        # BELOW the base as an optional runner rung, but the primary target is the base.
        target = pump_low * (1.0 + _B_TP_INSIDE_FRAC)
        # Best short entry = near the peak. Price has already faded a bit, so anchor
        # to a pullback toward the swept high (a limit above current on the dead-cat
        # bounce), not the already-lower current close — a short fills better higher.
        cur = float(meso_df["close"][-1])
        sweep_ext = float(data.get("sweep_extreme") or 0.0)
        entry_ref = max(cur, (cur + sweep_ext) / 2.0) if sweep_ext > cur else cur
        # Stop anchor = the TRUE manipulation high (pump_high), not the local sweep
        # wick — the stop must sit above the whole pump ("стоп за хая с запасом"),
        # else it lands inside the noise and a re-sweep kills it before the dump.
        stop_anchor = max(sweep_ext, pump_high_f)
        # Single target = the impulse-low (full pump absorption). We deliberately do
        # NOT append deeper distance-ranked pools: `_geometry` sets primary_target =
        # min(ladder) for shorts, so any deeper rung would pull the runner target back
        # out to the distant pool and re-introduce the 57-timeout / fantasy-RR failure
        # the impulse-low target fixed (dataset_v10: timeouts 57→14, RR sane).
        ladder = [target]
        setup = _build_setup(
            pattern_type="B", direction="short", meso_tf=meso_tf,
            swept_level=float(data.get("swept_level") or 0.0),
            sweep_extreme=stop_anchor,
            target=target,
            target_ladder=tuple(ladder),
            entry_ref=entry_ref,
            evidence=["sweep_above", fade_kind, "absorption",
                      "ltf_confirmed" if ltf_confirmed else "ltf_pending"]
                     + ([f"htf_{htf_bias}"] if htf_bias else []),
            total_steps=_B_TOTAL_STEPS,
            micro_confirmed=ltf_confirmed,
            micro_tf=micro_tf,
        )
        # Honest confidence: a pre-break (ltf_pending) peak short is a FORECAST, not
        # a confirmed break — reflect that in score + steps so the message doesn't
        # read as fully confirmed. A later LTF break would land as ltf_confirmed.
        setup.steps_covered = 3 if ltf_confirmed else 2
        setup.score = 1.0 if ltf_confirmed else 0.7
        setup.macro_tf = macro_tf  # реальный macro ТФ лестницы, не хардкод «1d»
        # Funding conviction/timing (non-gating): crowded-long squeeze fuel +
        # rollover = "основной слив" timing. This is what lets a pre-break
        # (ltf_pending 0.7) EARLY short earn conviction ONLY when funding confirms
        # the squeeze — the discriminator the mechanical top-picker (Pattern C)
        # lacked. Steps (price-structure fact) unchanged; score reflects conviction.
        f_ev, f_bump, f_note = _funding_short_signal(funding_ctx)
        if f_ev:
            setup.evidence = tuple(setup.evidence) + tuple(f_ev)
            setup.score = round(min(1.0, setup.score + f_bump), 3)
            setup.funding_note = f_note
        return new_symbol_state(), setup

    return new_symbol_state(), None


# ── Pattern C (long): break above prior swing high with volume confirmation
#    The author's type-2 long — "закреп выше предыдущего максимума" — a confirmed
#    close/break above the prior swing high, validated by bullish volume.
_C_TOTAL_STEPS = 2
# закреп = a HELD reclaim, not a one-candle poke. The transcript is explicit that
# without a full закреп «цена может пойти дальше вниз» — so require ≥2 consecutive
# closes holding above the prior high before the reclaim counts. Backtest motive:
# the old window=1 break entered at the extended breakout close with a far stop and
# went 5W/50L (9%) — the biggest loss source on the 45-symbol set. Entering the
# RETEST of the reclaimed level instead makes the stop structural (just under the
# reclaimed high) and filters breakouts that run away and fail without ever holding.
_C_ZAKREP_MIN_CLOSES = 2


def _consecutive_closes_above(df: pl.DataFrame, level: float) -> int:
    """Count trailing consecutive closes above ``level`` (adaptive buffer)."""
    if len(df) < 1 or level <= 0:
        return 0
    df_c = compute_features(df)
    buf = _adaptive_buffer(df_c)
    thresh = level * (1.0 + buf)
    count = 0
    for c in reversed(df_c["close"].to_list()):
        if c > thresh:
            count += 1
        else:
            break
    return count


def _advance_pattern_c(
    macro_df: pl.DataFrame, meso_df: pl.DataFrame, meso_tf: str,
    micro_15m: pl.DataFrame | None, state: dict[str, Any], now_ms: float,
    *,
    micro_tf: str = "15m",
    htf_df: pl.DataFrame | None = None,
    htf_bias: str | None = None,
    funding_ctx: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], ManipulationSetup | None]:
    stage = int(state.get("stage", 0))

    prev = _prior_swing_high(meso_df, lookback=60, exclude_last=15)
    if prev is None:
        return new_symbol_state(), None
    prior_high, prior_idx = prev

    # ── Stage 0/1: wait for a HELD reclaim (закреп), not a one-candle poke ──
    # The author's Type 2: ascending channel → peak → descending channel → bokovik
    # → закреп above the prior high → 20-50% move. A single break that immediately
    # rolls back is the fakeout he warns about; require ≥_C_ZAKREP_MIN_CLOSES
    # consecutive closes holding above the level.
    if stage == 0:
        zakrep = _consecutive_closes_above(meso_df, prior_high)
        if zakrep < _C_ZAKREP_MIN_CLOSES:
            return {
                "pattern": "C", "stage": 0, "anchor_ts": now_ms, "first_ts": now_ms,
                "meso_tf": meso_tf, "data": {"prior_high": prior_high, "prior_idx": prior_idx},
            }, None
        # Require the Type 2 structure (ascending pre-peak, descending post-peak) and
        # bullish volume behind the reclaim — the author's закреп discriminator.
        pre_df = meso_df[:prior_idx] if prior_idx > 0 else meso_df.head(1)
        post_df = meso_df[prior_idx:]
        asc_ok = is_ascending_channel(pre_df, lookback=min(40, len(pre_df)), min_swings=2)
        desc_ok = is_descending_channel(post_df, lookback=min(40, len(post_df)), min_swings=2)
        if not asc_ok or not desc_ok:
            return new_symbol_state(), None
        vol_ok = bullish_volume(meso_df) or (micro_15m is not None and bullish_volume(micro_15m))
        if not vol_ok:
            return new_symbol_state(), None

        # ── Enter on the закреп — this is the MANIPULATION module, NOT Prizrak. ──
        # A held reclaim (≥2 closes) with volume is the entry event; we do NOT wait for a
        # tight retest of the level (that is Prizrak-precision and leaves no room for the
        # module's добор + пересиживание). The stop is WIDE: anchored below the LOW of the
        # post-peak manipulation structure (the descending-channel / боковик низ), not just
        # under the reclaimed high — so an adverse dip can be averaged into and sat through
        # to a 20%+ pool, exactly the module's design. The _geometry RR gate filters out
        # setups where that wide stop makes the 20% target un-payable.
        struct_low = _to_float(post_df["low"].min()) if len(post_df) else float(meso_df["low"][-1])
        entry_ref = float(meso_df["close"][-1])  # the закреп close
        if struct_low <= 0 or struct_low >= entry_ref:
            struct_low = prior_high  # degenerate — fall back to the reclaimed level
        # REACHABLE target = measured move (entry + manipulation amplitude), NOT the
        # distance-ranked pool ladder. `_target_ladder[0]` is ≥20%-floored + farther-
        # biased → fantasy-RR / timeouts, the exact pathology Pattern B already fixed
        # to the impulse-low. Validated on full dataset_v11 (backtest_c_target.py): the
        # measured-move target flips C avgR −0.484 → +0.127 (losses 102→67, timeouts
        # 46→28). Single target, no deep pools (a deeper rung re-inflates the RR — same
        # reason B keeps ladder=[target]); fall back to the ladder only if degenerate.
        c_target = _measured_move_target(meso_df, entry_ref)
        if c_target <= entry_ref:
            _lad = _target_ladder(macro_df, meso_df, entry=entry_ref, direction="long", htf_df=htf_df,
                                  funding_mult=_funding_target_mult("long", funding_ctx))
            c_target = _lad[0] if _lad else _to_float(meso_df["high"].max())
        micro_df = _micro_df(micro_15m, meso_df)
        ltf_confirmed = bool(bos_up(micro_df) or choch_bull(micro_df))
        # «Почему» must DISCRIMINATE, not restate preconditions. asc_ok/desc_ok/zakrep
        # are hard gates above (`if not …: return`), so those tokens are present on EVERY
        # emitted C → tautological (BILL=CRV=HEI verbatim). Lead with the VARYING factors
        # — reclaim depth, micro-confirmation state, HTF bias — so two C setups read
        # differently; the fixed preconditions follow as context. Also emit the canonical
        # htf_bull/htf_bear tokens (not htf_bullish) so _split_evidence classes a
        # counter-bias correctly, and the ltf token C previously lacked. (WO #4)
        evidence = [
            f"закреп×{zakrep}",
            "ltf_confirmed" if ltf_confirmed else "ltf_pending",
            "prior_swing_high", "zakrep_reclaim", "wide_stop_dobor",
            "ascending_channel_pre" if asc_ok else "",
            "descending_channel_post" if desc_ok else "",
        ]
        if htf_bias == "bull":
            evidence.append("htf_bull")
        elif htf_bias == "bear":
            evidence.append("htf_bear")
        setup = _build_setup(
            pattern_type="C", direction="long", meso_tf=meso_tf,
            swept_level=prior_high,
            sweep_extreme=struct_low,  # WIDE stop anchor: below the manipulation low
            target=c_target,
            target_ladder=(c_target,),
            entry_ref=entry_ref,
            evidence=[e for e in evidence if e],
            total_steps=_C_TOTAL_STEPS,
            micro_confirmed=ltf_confirmed,
            micro_tf=micro_tf,
        )
        setup.score = 1.0 if ltf_confirmed else 0.7
        setup.steps_covered = _C_TOTAL_STEPS if ltf_confirmed else _C_TOTAL_STEPS - 1
        setup.macro_tf = meso_tf
        return new_symbol_state(), setup

    return new_symbol_state(), None


def advance_manipulation_state(
    symbol: str,
    ohlcv_by_tf: dict[str, list[list[float]]],
    state: dict[str, Any] | None,
    *,
    now_ms: float | None = None,
    macro_tf: str = _MACRO_TF,
    meso_tf: str | None = None,
    micro_tf: str = "15m",
    family: str | None = None,
    funding_ctx: dict[str, Any] | None = None,
    htf_bias: str | None = None,
) -> tuple[dict[str, Any], ManipulationSetup | None]:
    """Advance ``symbol``'s tracked pattern by at most one stage, for ONE TF ladder.

    ``macro_tf``/``meso_tf``/``micro_tf`` select the scale. If ``meso_tf`` is None
    it auto-picks the first available of ``_MESO_TF_PRIORITY`` (legacy behavior).
    ``family`` restricts to one pattern family ("A" long or "B" short); None runs
    both against a shared state (legacy). Running A and B on SEPARATE states (the
    multi-scale driver does this) matters: a short manipulation's opening pump is
    an up-impulse that starts Pattern A — on a shared state that blocks Pattern B
    (the dump) from ever tracking the same move. Independent states let both
    progress; emission arbitrates.

    Callers wanting full multi-scale coverage use ``advance_manipulation_scales``.

    Returns (new_state_to_persist, setup_if_just_completed_or_None). Callers
    persist the returned state across scan cycles (see state.py) — this
    function must never be called with a fresh reset_state() on every cycle,
    or the whole point of the redesign is lost.
    """
    import time
    if now_ms is None:
        now_ms = time.time() * 1000

    state = state or new_symbol_state()
    if is_stale(state, now_ms=now_ms):
        state = new_symbol_state()

    macro_raw = ohlcv_by_tf.get(macro_tf)
    if not macro_raw or len(macro_raw) < _MACRO_MIN_BARS.get(macro_tf, _MACRO_MIN_BARS_DEFAULT):
        return state, None
    macro_df = ohlcv_to_df(macro_raw)

    if meso_tf is None:
        meso_tf = next((tf for tf in _MESO_TF_PRIORITY if ohlcv_by_tf.get(tf)), None)
    if meso_tf is None or not ohlcv_by_tf.get(meso_tf):
        return state, None
    meso_df = ohlcv_to_df(ohlcv_by_tf[meso_tf])
    if len(meso_df) < _MESO_RECENT_CANDIDATES + 1:
        return state, None

    micro_15m = ohlcv_to_df(ohlcv_by_tf[micro_tf]) if ohlcv_by_tf.get(micro_tf) else None

    # Grab the highest available macro TF for structural target ladders.
    # 1d is ideal; fall back to 4h if daily data hasn't filled in yet.
    htf_raw = ohlcv_by_tf.get("1d") or ohlcv_by_tf.get("4h")
    htf_df = ohlcv_to_df(htf_raw) if htf_raw else None

    pattern = state.get("pattern")
    if family == "A":
        return _advance_pattern_a(macro_df, meso_df, meso_tf, micro_15m, state, now_ms,
                                  micro_tf=micro_tf, htf_df=htf_df, htf_bias=htf_bias, funding_ctx=funding_ctx)
    if family == "B":
        return _advance_pattern_b(macro_df, meso_df, meso_tf, micro_15m, state, now_ms,
                                  macro_tf=macro_tf, micro_tf=micro_tf, funding_ctx=funding_ctx, htf_df=htf_df,
                                  htf_bias=htf_bias)
    if family == "C":
        return _advance_pattern_c(macro_df, meso_df, meso_tf, micro_15m, state, now_ms,
                                  micro_tf=micro_tf, htf_df=htf_df, htf_bias=htf_bias, funding_ctx=funding_ctx)

    # Legacy shared-state path (family=None): try A, B, C on one state. Kept for
    # the stateless detect_manipulation_setup wrapper; the multi-scale driver runs
    # A, B, C on independent states instead (see advance_manipulation_scales).
    if pattern in (None, "A", "A3"):
        new_state, setup = _advance_pattern_a(macro_df, meso_df, meso_tf, micro_15m, state, now_ms,
                                              micro_tf=micro_tf, htf_df=htf_df, htf_bias=htf_bias, funding_ctx=funding_ctx)
        if setup is not None or new_state.get("pattern") in ("A", "A3"):
            return new_state, setup

    if pattern in (None, "B") or state.get("pattern") in (None, "B"):
        new_state, setup = _advance_pattern_b(macro_df, meso_df, meso_tf, micro_15m, state, now_ms,
                                              macro_tf=macro_tf, micro_tf=micro_tf, funding_ctx=funding_ctx, htf_df=htf_df,
                                              htf_bias=htf_bias)
        return new_state, setup

    if pattern in (None, "C") or state.get("pattern") in (None, "C"):
        new_state, setup = _advance_pattern_c(macro_df, meso_df, meso_tf, micro_15m, state, now_ms,
                                              micro_tf=micro_tf, htf_df=htf_df, htf_bias=htf_bias, funding_ctx=funding_ctx)
        return new_state, setup

    return new_symbol_state(), None


def advance_manipulation_scales(
    symbol: str,
    ohlcv_by_tf: dict[str, list[list[float]]],
    states: dict[str, dict[str, Any]] | None,
    *,
    now_ms: float | None = None,
    funding_ctx: dict[str, Any] | None = None,
) -> tuple[dict[str, dict[str, Any]], ManipulationSetup | None]:
    """Run every TF ladder for one symbol, each with its own persisted state.

    ``states`` maps meso_tf -> that ladder's persisted state dict (the per-symbol
    value the delivery layer stores). Returns (updated_states, first_setup). A
    setup completing on any ladder is returned immediately; all ladders' states
    are advanced and persisted regardless so multi-stage progress isn't lost.
    """
    states = dict(states or {})
    completed: list[ManipulationSetup] = []
    for macro_tf, meso_tf, micro_tf in _TF_LADDERS:
        if not ohlcv_by_tf.get(macro_tf) or not ohlcv_by_tf.get(meso_tf):
            continue
        # Higher-TF trend bias constrains lower ladder direction:
        #   (1d,4h,15m) → no higher TF, bias = None
        #   (4h,1h,15m) → bias from 1d
        #   (1h,15m,5m) → bias from 4h
        bias_tf = {"1w": None, "1d": "1w", "4h": "1d", "1h": "4h"}.get(macro_tf)
        htf_bias = _htf_trend_bias(ohlcv_to_df(ohlcv_by_tf[bias_tf])) if bias_tf and ohlcv_by_tf.get(bias_tf) else None
        for family in ("A", "B", "C"):
            key = f"{meso_tf}:{family}"
            new_state, setup = advance_manipulation_state(
                symbol, ohlcv_by_tf, states.get(key), now_ms=now_ms,
                macro_tf=macro_tf, meso_tf=meso_tf, micro_tf=micro_tf, family=family,
                funding_ctx=funding_ctx, htf_bias=htf_bias,
            )
            states[key] = new_state
            if setup is not None:
                completed.append(setup)
    # Arbitrate when multiple scales/families complete on the same tick. SCALE
    # decides first: «лучше всего начинать анализ со старших таймфреймов и
    # постепенно переходить к младшим» — the higher-TF setup is the structural
    # read and the lower-TF one is usually a move inside it. Sorting by direction
    # first (as this did) let a 15m long outrank a 4h short, inverting the very
    # hierarchy the method is built on: the transcript's GTC short IS the higher-TF
    # move, not a shakeout inside a pump. Long only breaks a tie at EQUAL scale,
    # where a confirmed accumulation/break outranks the shakeout short.
    if not completed:
        return states, None
    _tf_rank = {"1w": 6, "1d": 5, "4h": 4, "1h": 3, "15m": 2, "5m": 1, "3m": 0}
    completed.sort(
        key=lambda s: (-_tf_rank.get(s.meso_tf, 0), 0 if s.direction == "long" else 1)
    )
    return states, completed[0]


def detect_manipulation_setup(
    ohlcv_by_tf: dict[str, list[list[float]]],
    *,
    cfg: Any = None,
) -> ManipulationSetup | None:
    """Stateless convenience wrapper — advances a throwaway state by one
    stage and returns a setup only if that single call happens to complete
    the whole sequence. Real callers (deliver/manipulation_delivery.py)
    should use advance_manipulation_scales with persisted state so
    multi-stage patterns aren't lost between scan cycles.
    """
    _, setup = advance_manipulation_scales("", ohlcv_by_tf, None)
    return setup


__all__ = [
    "Direction", "ManipulationSetup",
    "advance_manipulation_state", "advance_manipulation_scales",
    "detect_manipulation_setup",
]
