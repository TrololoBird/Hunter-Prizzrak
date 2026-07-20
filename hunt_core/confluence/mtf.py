"""MTF family-voting confluence (P6 — extracted from deep_signal)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from hunt_core.features.models import FeaturePanel
    from hunt_core.view.models import MarketView

# Stop must clear the anchoring level by at least this much ATR, and never sit
# closer than the nominal percentage floor shared with levels.py.
_MIN_STOP_ATR = 1.0


def _min_sl_pct(symbol: str, direction: str) -> float:
    from hunt_core.levels.levels import long_min_sl_dist_pct, short_min_sl_dist_pct

    return long_min_sl_dist_pct(symbol) if direction == "long" else short_min_sl_dist_pct(symbol)

@dataclass
class TFSignal:
    tf: str
    trend: Literal["bull", "bear", "neutral"]
    rsi14: float
    adx14: float
    label: str
    # Level-A zero-degradation: distinguish a real ranging read ("neutral", EMAs
    # present but interwoven) from "we could not determine trend because the
    # timeframe lacks enough history to warm the EMA stack" (insufficient_history).
    # Both used to surface identically as "нейтр", hiding data degradation.
    data_quality: Literal["ok", "insufficient_history"] = "ok"


@dataclass
class ScenarioScore:
    direction: Literal["long", "short"]
    score: float            # 0..1
    htf_count: int          # how many of 1W/1D/4H align with this direction
    htf_total: int          # how many HTF TFs had data
    entry_lo: float
    entry_hi: float
    tp1: float
    tp2: float
    stop: float
    evidence: list[str] = field(default_factory=list)


@dataclass
class MTFConfluence:
    symbol: str
    price: float
    tf_signals: dict[str, TFSignal]
    long_scenario: ScenarioScore
    short_scenario: ScenarioScore
    dominant: Literal["long", "short", "neutral"]

    def to_dict(self) -> dict[str, Any]:
        return mtf_confluence_to_dict(self)


_DISPLAY_TFS = ["1w", "1d", "4h", "15m"]
_HTF_TFS = ["1w", "1d", "4h"]


def _trend_from_snap(snap: dict[str, Any]) -> Literal["bull", "bear", "neutral"]:
    # Prefer the canonical EMA-stack computation over the cached "trend" string.
    # The cached value may be "mixed" (neutral) even when close < ema20 < ema50
    # (post-pump dump: ema200 still below due to pre-pump history, so the full
    # 4-EMA bear stack fails despite a clear 3-EMA bearish alignment).
    from hunt_core.toolkit.trend import trend_from_snapshot

    recomputed = trend_from_snapshot(snap, require_adx=False)
    if recomputed in ("bull", "bear"):
        return recomputed  # type: ignore[return-value]
    # Fall back to cached label (catches old snapshots with pre-computed trend).
    t = snap.get("trend") or ""
    if t == "bull":
        return "bull"
    if t == "bear":
        return "bear"
    return "neutral"


def _tf_data_quality(snap: dict[str, Any]) -> Literal["ok", "insufficient_history"]:
    """A TF whose snapshot lacks a warmed EMA stack cannot yield a real trend.

    The HTF frames fall back to ``tf_snapshot_lite`` (status="lite", no ema20/ema50)
    when there are too few bars to warm EMA200 in ``_prepare_frame``. Treat any
    snapshot without at least ema20+ema50 as insufficient history, not "ranging".
    """
    if str(snap.get("status") or "") == "lite":
        return "insufficient_history"
    e20 = snap.get("ema20")
    e50 = snap.get("ema50")
    if e20 is None or e50 is None or float(e20 or 0) <= 0 or float(e50 or 0) <= 0:
        return "insufficient_history"
    return "ok"


def _tf_label(snap: dict[str, Any], trend: str) -> str:
    adx = float(snap.get("adx14") or 0)
    sup = snap.get("supertrend_dir")
    rsi = float(snap.get("rsi14") or 50)
    if _tf_data_quality(snap) == "insufficient_history":
        return "недостаточно истории"
    if trend == "bull":
        if adx >= 25:
            return "Сильный бычий тренд"
        if sup == 1:
            return "Supertrend бычий"
        return "Выше EMA50"
    if trend == "bear":
        if adx >= 25:
            return "Сильный медвежий тренд"
        if sup == -1:
            return "Supertrend медвежий"
        return "Ниже EMA50"
    if rsi > 62:
        return "Импульс восходящий"
    if rsi < 38:
        return "Импульс нисходящий"
    return "EMA переплетены"


def _rsi_edge(rsi: float, direction: str) -> float:
    """0..1 momentum edge for the given direction from RSI."""
    if direction == "long":
        return max(0.0, min(1.0, (rsi - 40.0) / 30.0))
    return max(0.0, min(1.0, (60.0 - rsi) / 30.0))


def build_mtf_confluence(
    symbol: str,
    tf: dict[str, Any],
    price: float,
    *,
    market: dict[str, Any] | None = None,
    row: dict[str, Any] | None = None,
) -> MTFConfluence:
    """
    Build MTF confluence from row['timeframes'] (already contains per-TF snapshots).

    Args:
        symbol: e.g. "BTCUSDT"
        tf: row["timeframes"] dict — keys "1w","1d","4h","15m","1h",…
        price: current mark price
    """
    tf_signals: dict[str, TFSignal] = {}
    for key in _DISPLAY_TFS:
        snap = tf.get(key) or {}
        if not snap or snap.get("status") == "empty":
            continue
        trend = _trend_from_snap(snap)
        rsi = float(snap.get("rsi14") or 50)
        adx = float(snap.get("adx14") or 0)
        dq = _tf_data_quality(snap)
        tf_signals[key] = TFSignal(
            tf=key,
            trend=trend,
            rsi14=rsi,
            adx14=adx,
            label=_tf_label(snap, trend),
            data_quality=dq,
        )

    # ATR from best available TF for level placement
    atr = 0.0
    for k in ("4h", "1d", "1h", "15m"):
        v = float((tf.get(k) or {}).get("atr14") or 0)
        if v > 0:
            atr = v
            break
    if atr <= 0:
        atr = price * 0.01

    def _build(direction: str) -> ScenarioScore:
        # HTF score — only count TFs with a determinate trend (bull or bear).
        # A neutral TF (no EMA data in fast-tier scan) provides zero signal and
        # should not inflate htf_total: that would make family_vote_low fire when
        # the only available HTF (4H) correctly aligns with direction.
        htf_aligned = 0
        htf_total = 0
        evidence: list[str] = []
        for k in _HTF_TFS:
            sig = tf_signals.get(k)
            if sig is None or sig.trend == "neutral":
                continue
            htf_total += 1
            ok = (direction == "long" and sig.trend == "bull") or (
                direction == "short" and sig.trend == "bear"
            )
            if ok:
                htf_aligned += 1
                evidence.append(f"{k.upper()}: {sig.label}")

        htf_ratio = htf_aligned / htf_total if htf_total else 0.0

        # LTF momentum (15M, fallback 1H)
        ltf_snap = tf.get("15m") or tf.get("1h") or {}
        ltf_rsi = float(ltf_snap.get("rsi14") or 50)
        ltf_edge = _rsi_edge(ltf_rsi, direction)

        score = round(htf_ratio * 0.60 + ltf_edge * 0.40, 3)

        # Structural geometry from targets — no ATR fallback
        from hunt_core.toolkit.targets import (
            collect_downward_targets as _cdt,
            collect_upward_targets as _cut,
        )

        _structure = (row or {}).get("structure")
        structure = _structure if isinstance(_structure, dict) else {}
        _kl = structure.get("key_levels")
        kl = _kl if isinstance(_kl, dict) else {}
        _pools = (row or {}).get("liquidity_pools")
        pools = _pools if isinstance(_pools, dict) else {}

        # Minimum stop distance. Anchoring the stop straight onto the nearest
        # support/resistance leaves no room when that level sits right under the
        # price — which is the normal case, since the level is *why* price is
        # there. Those signals stop out on noise: in signal_history.jsonl every
        # entry whose nominal risk was under 0.5% closed stop_hit (26/26), a third
        # of them without the price ever ticking in favour. Push the stop past the
        # level by at least the same floor structural_long_levels/structural_short_levels
        # already enforce; the percentage term also covers a degenerate ATR of 0.
        min_dist = max(_MIN_STOP_ATR * atr, price * _min_sl_pct(symbol, direction) / 100.0)

        if direction == "long":
            entry_lo = price - 0.3 * atr
            entry_hi = price + 0.3 * atr
            tgts, _ = _cut(row or {}, price)
            tp1 = tgts[0] if len(tgts) > 0 else 0.0
            tp2 = tgts[1] if len(tgts) > 1 else tp1
            sup = float(kl.get("support") or kl.get("last_swing_low") or pools.get("nearest_below") or 0)
            raw_stop = sup if sup > 0 and sup < price else price - 2.0 * atr
            stop = min(raw_stop, price - min_dist)  # never closer than the floor
        else:
            entry_lo = price - 0.3 * atr
            entry_hi = price + 0.3 * atr
            tgts, _ = _cdt(row or {}, price)
            tp1 = tgts[0] if len(tgts) > 0 else 0.0
            tp2 = tgts[1] if len(tgts) > 1 else tp1
            res = float(kl.get("resistance") or kl.get("last_swing_high") or pools.get("nearest_above") or 0)
            raw_stop = res if res > 0 and res > price else price + 2.0 * atr
            stop = max(raw_stop, price + min_dist)

        if htf_total:
            evidence.insert(0, f"HTF {htf_aligned}/{htf_total}")

        return ScenarioScore(
            direction=direction,  # type: ignore[arg-type]
            score=score,
            htf_count=htf_aligned,
            htf_total=htf_total,
            entry_lo=round(entry_lo, 6),
            entry_hi=round(entry_hi, 6),
            tp1=round(tp1, 6),
            tp2=round(tp2, 6),
            stop=round(stop, 6),
            evidence=evidence,
        )

    long_s = _build("long")
    short_s = _build("short")


    if long_s.score >= short_s.score + 0.15:
        dominant: Literal["long", "short", "neutral"] = "long"
    elif short_s.score >= long_s.score + 0.15:
        dominant = "short"
    else:
        dominant = "neutral"

    if row is not None:
        _dump = row.get("dump")
        dump = _dump if isinstance(_dump, dict) else {}
        _long_setup = row.get("long")
        long_setup = _long_setup if isinstance(_long_setup, dict) else {}
        short_ok = dump.get("levels_viable") is not False
        long_ok = long_setup.get("levels_viable") is not False
        if not short_ok or not long_ok:
            dominant = "neutral"

    return MTFConfluence(
        symbol=symbol,
        price=price,
        tf_signals=tf_signals,
        long_scenario=long_s,
        short_scenario=short_s,
        dominant=dominant,
    )


# Level C — MTF-first direction. Higher timeframes carry more directional weight.
# 1W/1D define the regime; 4H is a swing modifier. LTF (1h/15m) is timing only and
# deliberately excluded from the bias.
_HTF_BIAS_WEIGHTS = {"1w": 0.45, "1d": 0.35, "4h": 0.20}
_HTF_BIAS_THRESHOLD = 0.30  # net weighted alignment needed to call a directional bias


def htf_bias_from_signals(
    tf_signals: dict[str, TFSignal],
) -> dict[str, Any]:
    """Aggregate 1W/1D/4H into a single directional bias (Level C).

    Only timeframes with ``data_quality == "ok"`` and a determinate bull/bear trend
    vote. Returns the net weighted score, the resolved bias, and how much HTF weight
    was actually available (so callers can tell "conflicting" from "no HTF data").
    """
    net = 0.0
    weight_available = 0.0
    votes: dict[str, str] = {}
    for tf_key, w in _HTF_BIAS_WEIGHTS.items():
        sig = tf_signals.get(tf_key)
        if sig is None or sig.data_quality != "ok":
            continue
        weight_available += w
        if sig.trend == "bull":
            net += w
            votes[tf_key] = "bull"
        elif sig.trend == "bear":
            net -= w
            votes[tf_key] = "bear"
        else:
            votes[tf_key] = "neutral"
    # Normalise against the weight that was actually available so a single valid
    # HTF (e.g. only 4H warmed on a young listing) can still express a bias, but a
    # symbol with no warmed HTF resolves to "unknown", never a false "neutral".
    norm = (net / weight_available) if weight_available > 0 else 0.0
    if weight_available <= 0.0:
        bias = "unknown"
    elif norm >= _HTF_BIAS_THRESHOLD:
        bias = "long"
    elif norm <= -_HTF_BIAS_THRESHOLD:
        bias = "short"
    else:
        bias = "neutral"
    return {
        "bias": bias,
        "score": round(norm, 3),
        "weight_available": round(weight_available, 3),
        "votes": votes,
    }


def _scenario_to_dict(sc: ScenarioScore) -> dict[str, Any]:
    return {
        "direction": sc.direction,
        "score": round(float(sc.score), 4),
        "htf_count": int(sc.htf_count),
        "htf_total": int(sc.htf_total),
        "entry_lo": float(sc.entry_lo),
        "entry_hi": float(sc.entry_hi),
        "tp1": float(sc.tp1),
        "tp2": float(sc.tp2),
        "stop": float(sc.stop),
        "evidence": list(sc.evidence),
    }


_NATIVE_MTF_TFS = ("1w", "1d", "4h", "1h", "15m")


def build_mtf_confluence_native(
    view: MarketView, features: FeaturePanel
) -> MTFConfluence:
    """MTF confluence from the typed native handles (ADR-0004 main-tick MTF consumer).

    Projects the per-TF :class:`~hunt_core.features.models.TfSummary` handles onto the minimal
    per-TF indicator snapshot :func:`build_mtf_confluence` reads (rsi/adx/atr/EMA-stack/trend), then
    delegates to the shared arithmetic. Structural targets (``row["structure"]`` /
    ``liquidity_pools``) live on the deep/prizrak output, not on the main tick, so this journal-only
    confluence gets ATR-anchored stops and empty targets (``row`` omitted) — the meaningful part
    (tf_signals, HTF alignment counts, dominant bias) is fully faithful.
    """
    symbol = str(view.symbol or "").split(":", 1)[0].replace("/", "").upper()
    tf_dict: dict[str, Any] = {}
    for key in _NATIVE_MTF_TFS:
        s = features.tf.get(key)
        if s is None:
            continue
        tf_dict[key] = {
            "rsi14": s.rsi14,
            "adx14": s.adx14,
            "atr14": s.atr14,
            "ema20": s.ema20,
            "ema50": s.ema50,
            "ema200": s.ema200,
            "close": s.close,
            "supertrend_dir": s.supertrend_dir,
            "trend": s.trend,
        }
    return build_mtf_confluence(symbol, tf_dict, float(view.last_price))


def mtf_confluence_to_dict(mtf: MTFConfluence) -> dict[str, Any]:
    """JSONL-safe MTF payload with HTF counts for ``family_vote_count`` replay."""
    return {
        "symbol": mtf.symbol,
        "price": float(mtf.price),
        "dominant": mtf.dominant,
        "long_scenario": _scenario_to_dict(mtf.long_scenario),
        "short_scenario": _scenario_to_dict(mtf.short_scenario),
        "long_htf_count": int(mtf.long_scenario.htf_count),
        "short_htf_count": int(mtf.short_scenario.htf_count),
        "long_htf_total": int(mtf.long_scenario.htf_total),
        "short_htf_total": int(mtf.short_scenario.htf_total),
        "tf_trends": {
            k: {"trend": s.trend, "data_quality": s.data_quality, "label": s.label}
            for k, s in mtf.tf_signals.items()
        },
        "htf_bias": htf_bias_from_signals(mtf.tf_signals),
    }



