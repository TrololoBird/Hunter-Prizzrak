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

from hunt_core.scanner.detect.events import (
    ohlcv_to_df, compute_features,
    detect_impulse, detect_consecutive_impulse,
    detect_absorption,
    detect_bokovik, detect_sweep_low, detect_sweep_high,
    candle_fade_ratio, rejection_at_peak,
    two_bar_reversal,
    bos_up, bos_down, choch_bull, choch_bear,
    bullish_volume,
    break_above_level_recent,
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
    ("1d", "4h", "15m"),
    ("4h", "1h", "15m"),
    ("1h", "15m", "5m"),
)


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
        return float(window["high"].max())
    return float(window["low"].min())


def _prior_swing_high(df: pl.DataFrame, lookback: int = 60, *, exclude_last: int = 15) -> float | None:
    """Most recent swing high BEFORE the pump (exclude last N bars = the impulse itself)."""
    body = df.tail(lookback)[:-exclude_last] if exclude_last > 0 else df.tail(lookback)
    if len(body) < 10:
        return None
    df_c = compute_features(body)
    swing_vals = df_c.filter(pl.col("_swing_high"))["high"]
    if swing_vals.is_empty():
        return None
    return float(swing_vals.tail(1)[0])


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


def _target_ladder(
    macro_df: pl.DataFrame, meso_df: pl.DataFrame, *, entry: float, direction: Direction,
    max_targets: int = 6,
) -> list[float]:
    """Full structural target ladder — the liquidity pools the move travels through
    (course: «много ликвидности снизу … среднесрочная цель», sequential take-profits),
    NOT a single 30% retrace. Short → swing LOWS below entry (nearest first); long →
    swing HIGHS above. Pulled from macro+meso pivots, deduped within 1.5%.
    """
    levels: list[float] = []
    for df in (meso_df, macro_df):
        if df is None or df.height < 12:
            continue
        feat = compute_features(df)
        col = "low" if direction == "short" else "high"
        swing_col = "_swing_low" if direction == "short" else "_swing_high"
        vals = feat.filter(pl.col(swing_col))[col]
        for v in vals:
            fv = float(v)
            if direction == "short" and fv < entry:
                levels.append(fv)
            elif direction == "long" and fv > entry:
                levels.append(fv)
    if not levels:
        return []
    levels = sorted(set(levels), reverse=(direction == "short"))
    out: list[float] = []
    for lv in levels:  # nearest-first, dedupe within 1.5%
        if not out or abs(lv - out[-1]) / max(abs(out[-1]), 1e-9) >= 0.015:
            out.append(lv)
        if len(out) >= max_targets:
            break
    return out


def _build_setup(
    *, pattern_type: PatternType, direction: Direction, meso_tf: str,
    swept_level: float, sweep_extreme: float, target: float | None,
    entry_ref: float | None, evidence: list[str], total_steps: int,
    target_ladder: tuple[float, ...] = (),
    micro_confirmed: bool = False,
) -> ManipulationSetup:
    return ManipulationSetup(
        direction=direction,
        pattern_type=pattern_type,
        score=full_confirmation_score(),
        meso_tf=meso_tf,
        micro_tf="15m",
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


# ── Pattern A (long): impulse -> absorption -> accumulation -> sweep -> break
#    Pattern A3 (long, no impulse): accumulation -> break
_A_TOTAL_STEPS = 5
_A3_TOTAL_STEPS = 2


def _advance_pattern_a(
    macro_df: pl.DataFrame, meso_df: pl.DataFrame, meso_tf: str,
    micro_15m: pl.DataFrame | None, state: dict[str, Any], now_ms: float,
) -> tuple[dict[str, Any], ManipulationSetup | None]:
    stage = int(state.get("stage", 0))
    pattern = state.get("pattern")
    data: dict[str, Any] = dict(state.get("data") or {})

    if stage == 0:
        imp_ok, imp_idx = detect_impulse(meso_df, lookback=30, direction="up")
        if not imp_ok:
            imp_ok, imp_idx = detect_consecutive_impulse(meso_df, min_count=3, direction="up")
        if imp_ok and imp_idx is not None:
            return {
                "pattern": "A", "stage": 1, "anchor_ts": now_ms, "first_ts": now_ms,
                "meso_tf": meso_tf, "data": {"impulse_idx": int(imp_idx)},
            }, None
        b1 = detect_bokovik(meso_df, window=_BOKOVIK_WINDOW)
        if b1 is not None:
            return {
                "pattern": "A3", "stage": 1, "anchor_ts": now_ms, "first_ts": now_ms,
                "meso_tf": meso_tf, "data": {"bokovik": b1},
            }, None
        return new_symbol_state(), None

    if pattern == "A":
        if stage == 1:
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
            # Variant A (predictive, symmetric to Pattern B): the long is taken at
            # the LOW of the (second) consolidation the moment its low is swept
            # ("вход у низа второй консолидации"), NOT after the 15m breaks up.
            # bos_up/choch_bull is a STRENGTH upgrade only (ltf_confirmed vs
            # ltf_pending). Stop below the swept low + averaging handle a deeper wick.
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
            micro_df = _micro_df(micro_15m, meso_df)
            ltf_confirmed = bool(bos_up(micro_df) or choch_bull(micro_df))
            # Volume confirmation gate (course: "если нету бычьих объёмов — лонг
            # не валиден"). Soft gate: lower score + flag evidence instead of
            # blocking entirely, so a valid geometry without a volume spike still
            # gets delivered but at reduced confidence.
            vol_ok = bullish_volume(meso_df) or (micro_15m is not None and bullish_volume(micro_15m))
            # swept_level = лоу боковика (реально свипнутый уровень на meso ТФ),
            # НЕ macro_low: тот — 1d-контекст, в сообщении читался как «свипнули
            # 1d-уровень 0.89», хотя свипнут 0.92 на 15m.
            a_entry = _consolidation_long_entry(meso_df, bokovik)
            a_ladder = _target_ladder(macro_df, meso_df, entry=a_entry, direction="long")
            evidence = ["impulse", "absorption", "bokovik", "sweep_below",
                        "ltf_confirmed" if ltf_confirmed else "ltf_pending"]
            if not vol_ok:
                evidence.append("volume_pending")
            setup = _build_setup(
                pattern_type="A", direction="long", meso_tf=meso_tf,
                swept_level=float(bokovik.get("lo") or 0.0),
                sweep_extreme=float(sweep_extreme),
                target=(a_ladder[0] if a_ladder else float(meso_df["high"].max())),
                target_ladder=tuple(a_ladder),
                entry_ref=a_entry,
                evidence=evidence,
                total_steps=_A_TOTAL_STEPS,
                micro_confirmed=ltf_confirmed,
            )
            base_score = 1.0 if ltf_confirmed else 0.7
            setup.score = base_score * 0.6 if not vol_ok else base_score
            setup.steps_covered = _A_TOTAL_STEPS if ltf_confirmed else _A_TOTAL_STEPS - 1
            setup.macro_tf = meso_tf
            return new_symbol_state(), setup

    if pattern == "A3" and stage == 1:
        # A3 (accumulation, no prior impulse): predictive long at the accumulation
        # FLOOR — emit only when price sits in the lower half of the боковик (buying
        # the floor, not chasing mid-range), ltf break = upgrade.
        bokovik = data.get("bokovik") or {}
        lo = float(bokovik.get("lo") or 0.0)
        hi = float(bokovik.get("hi") or 0.0)
        if lo <= 0 or hi <= lo:
            return new_symbol_state(), None
        cur = float(meso_df["close"][-1])
        if cur > (lo + hi) / 2.0:
            return state, None  # price mid/upper range — wait for a retest of the floor
        micro_df = _micro_df(micro_15m, meso_df)
        ltf_confirmed = bool(bos_up(micro_df) or choch_bull(micro_df))
        # A3 is a QUIET accumulation floor entry — low volume is the EXPECTED
        # state here (the "бычьи объёмы" confirm only on the later breakout).
        # So volume is informational only; it does NOT penalize the score the
        # way it does for impulse-driven Pattern A / C.
        vol_ok = bullish_volume(meso_df) or (micro_15m is not None and bullish_volume(micro_15m))
        a3_entry = _consolidation_long_entry(meso_df, bokovik)
        a3_ladder = _target_ladder(macro_df, meso_df, entry=a3_entry, direction="long")
        evidence = ["accumulation_no_impulse", "ltf_confirmed" if ltf_confirmed else "ltf_pending"]
        if not vol_ok:
            evidence.append("volume_pending")
        setup = _build_setup(
            pattern_type="A3", direction="long", meso_tf=meso_tf,
            swept_level=lo,
            sweep_extreme=lo,
            target=(a3_ladder[0] if a3_ladder else float(meso_df["high"].max())),
            target_ladder=tuple(a3_ladder),
            entry_ref=a3_entry,
            evidence=evidence,
            total_steps=_A3_TOTAL_STEPS,
            micro_confirmed=ltf_confirmed,
        )
        base_score = 1.0 if ltf_confirmed else 0.7
        setup.score = base_score  # A3: no volume penalty (quiet accumulation)
        setup.steps_covered = _A3_TOTAL_STEPS if ltf_confirmed else _A3_TOTAL_STEPS - 1
        setup.macro_tf = meso_tf
        return new_symbol_state(), setup

    return new_symbol_state(), None


# ── Pattern B (short): sweep of a prior high -> fade/rejection -> LTF break
_B_TOTAL_STEPS = 3


def _advance_pattern_b(
    macro_df: pl.DataFrame, meso_df: pl.DataFrame, meso_tf: str,
    micro_15m: pl.DataFrame | None, state: dict[str, Any], now_ms: float,
    macro_tf: str = _MACRO_TF,
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
        meso_top = float(meso_df["high"].tail(20).max())
        if meso_top < macro_high * 0.98:
            return new_symbol_state(), None
        sweep_target = macro_high

        sweep_ok, sweep_extreme, _ = detect_sweep_high(meso_df, sweep_target)
        if not sweep_ok:
            return new_symbol_state(), None
        pump_high = float(meso_df["high"].tail(20).max())
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
        pump_high = data.get("pump_high")
        if pump_high is None:
            return new_symbol_state(), None
        body_ratio, range_ratio = candle_fade_ratio(meso_df, n=8, peak_high=pump_high)
        fade_ok = body_ratio <= 0.50 and range_ratio <= 0.60
        reject_ok = rejection_at_peak(meso_df, pump_high)
        two_bar_ok = two_bar_reversal(meso_df, pump_high)
        if not (fade_ok or reject_ok or two_bar_ok):
            return state, None
        fade_kind = "candle_fade" if fade_ok else ("instant_rejection" if reject_ok else "two_bar_reversal")
        micro_df = _micro_df(micro_15m, meso_df)
        ltf_confirmed = bool(bos_down(micro_df) or choch_bear(micro_df))
        pump_high_f = float(pump_high)
        pump_low = float(meso_df["close"].tail(min(60, len(meso_df))).min())
        pump_range = pump_high_f - pump_low
        # 30% retrace target (course: first take-profit zone)
        target = pump_high_f - pump_range * 0.30 if pump_range > 0 else None
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
        ladder = _target_ladder(macro_df, meso_df, entry=entry_ref, direction="short")
        setup = _build_setup(
            pattern_type="B", direction="short", meso_tf=meso_tf,
            swept_level=float(data.get("swept_level") or 0.0),
            sweep_extreme=stop_anchor,
            target=(ladder[0] if ladder else target),
            target_ladder=tuple(ladder),
            entry_ref=entry_ref,
            evidence=["sweep_above", fade_kind, "ltf_confirmed" if ltf_confirmed else "ltf_pending"],
            total_steps=_B_TOTAL_STEPS,
            micro_confirmed=ltf_confirmed,
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


def _advance_pattern_c(
    macro_df: pl.DataFrame, meso_df: pl.DataFrame, meso_tf: str,
    micro_15m: pl.DataFrame | None, state: dict[str, Any], now_ms: float,
) -> tuple[dict[str, Any], ManipulationSetup | None]:
    prior_high = _prior_swing_high(meso_df, lookback=60, exclude_last=15)
    if prior_high is None:
        return new_symbol_state(), None

    break_ok = break_above_level_recent(meso_df, prior_high, window=1)

    if not break_ok:
        return {
            "pattern": "C", "stage": 1, "anchor_ts": now_ms, "first_ts": now_ms,
            "meso_tf": meso_tf,
            "data": {"prior_high": prior_high},
        }, None

    vol_ok = bullish_volume(meso_df) or (micro_15m is not None and bullish_volume(micro_15m))
    entry_ref = float(meso_df["close"][-1])
    ladder = _target_ladder(macro_df, meso_df, entry=entry_ref, direction="long")
    evidence = ["prior_swing_high", "break_above_prior_high"]
    if not vol_ok:
        evidence.append("volume_pending")
    ltf_confirmed = break_ok
    setup = _build_setup(
        pattern_type="C", direction="long", meso_tf=meso_tf,
        swept_level=prior_high,
        sweep_extreme=prior_high,
        target=(ladder[0] if ladder else float(meso_df["high"].max())),
        target_ladder=tuple(ladder),
        entry_ref=entry_ref,
        evidence=evidence,
        total_steps=_C_TOTAL_STEPS,
        micro_confirmed=ltf_confirmed,
    )
    base_score = 1.0 if ltf_confirmed else 0.7
    setup.score = base_score * 0.6 if not vol_ok else base_score
    setup.steps_covered = _C_TOTAL_STEPS if ltf_confirmed else _C_TOTAL_STEPS - 1
    setup.macro_tf = meso_tf
    return new_symbol_state(), setup


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
    if not macro_raw or len(macro_raw) < _MACRO_LOOKBACK_BARS // 2:
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

    pattern = state.get("pattern")
    if family == "A":
        return _advance_pattern_a(macro_df, meso_df, meso_tf, micro_15m, state, now_ms)
    if family == "B":
        return _advance_pattern_b(macro_df, meso_df, meso_tf, micro_15m, state, now_ms,
                                  macro_tf=macro_tf, funding_ctx=funding_ctx)
    if family == "C":
        return _advance_pattern_c(macro_df, meso_df, meso_tf, micro_15m, state, now_ms)

    # Legacy shared-state path (family=None): try A, B, C on one state. Kept for
    # the stateless detect_manipulation_setup wrapper; the multi-scale driver runs
    # A, B, C on independent states instead (see advance_manipulation_scales).
    if pattern in (None, "A", "A3"):
        new_state, setup = _advance_pattern_a(macro_df, meso_df, meso_tf, micro_15m, state, now_ms)
        if setup is not None or new_state.get("pattern") in ("A", "A3"):
            return new_state, setup

    if pattern in (None, "B") or state.get("pattern") in (None, "B"):
        new_state, setup = _advance_pattern_b(macro_df, meso_df, meso_tf, micro_15m, state, now_ms,
                                              macro_tf=macro_tf, funding_ctx=funding_ctx)
        return new_state, setup

    if pattern in (None, "C") or state.get("pattern") in (None, "C"):
        new_state, setup = _advance_pattern_c(macro_df, meso_df, meso_tf, micro_15m, state, now_ms)
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
        for family in ("A", "B", "C"):
            key = f"{meso_tf}:{family}"
            new_state, setup = advance_manipulation_state(
                symbol, ohlcv_by_tf, states.get(key), now_ms=now_ms,
                macro_tf=macro_tf, meso_tf=meso_tf, micro_tf=micro_tf, family=family,
                funding_ctx=funding_ctx,
            )
            states[key] = new_state
            if setup is not None:
                completed.append(setup)
    # Arbitrate when multiple scales/families complete on the same tick: a
    # confirmed long accumulation/break (Pattern A/A3) outranks a short — the
    # transcript's short setups are the shakeout INSIDE a larger pump, so when
    # both fire the long is the real move. Within a family, the higher-TF (larger
    # scale) setup wins as the more structural read.
    if not completed:
        return states, None
    _tf_rank = {"1w": 6, "1d": 5, "4h": 4, "1h": 3, "15m": 2, "5m": 1, "3m": 0}
    completed.sort(
        key=lambda s: (0 if s.direction == "long" else 1, -_tf_rank.get(s.meso_tf, 0))
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
