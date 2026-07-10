"""Per-symbol structural regime classifier with deterministic transitions.

Runs parallel to hunt lifecycle phases — does not drive phase logic.
State is latched per symbol via ``SymbolStateStore.regime``.
"""
from __future__ import annotations



from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

# Scalar fallback for dead-code path — avoids importing runtime.state.
_symbol_regime_local: dict[str, Any] = {}


Direction = Literal["short", "long"]


class Regime(StrEnum):
    TREND_UP = "trend_up"
    TREND_DOWN = "trend_down"
    RANGE = "range"
    SQUEEZE = "squeeze"
    EXPANSION = "expansion"
    CAPITULATION = "capitulation"
    EUPHORIA = "euphoria"


# Deterministic transition graph — only listed edges are allowed.
_ALLOWED_TRANSITIONS: dict[Regime, frozenset[Regime]] = {
    Regime.RANGE: frozenset(
        {Regime.TREND_UP, Regime.TREND_DOWN, Regime.SQUEEZE, Regime.EXPANSION}
    ),
    Regime.SQUEEZE: frozenset({Regime.EXPANSION, Regime.RANGE, Regime.TREND_UP, Regime.TREND_DOWN}),
    Regime.EXPANSION: frozenset(
        {Regime.TREND_UP, Regime.TREND_DOWN, Regime.EUPHORIA, Regime.CAPITULATION, Regime.RANGE}
    ),
    Regime.TREND_UP: frozenset({Regime.EUPHORIA, Regime.EXPANSION, Regime.RANGE, Regime.SQUEEZE}),
    Regime.TREND_DOWN: frozenset({Regime.CAPITULATION, Regime.EXPANSION, Regime.RANGE, Regime.SQUEEZE}),
    Regime.EUPHORIA: frozenset(
        {Regime.CAPITULATION, Regime.EXPANSION, Regime.TREND_DOWN, Regime.RANGE}
    ),
    Regime.CAPITULATION: frozenset({Regime.RANGE, Regime.TREND_UP, Regime.EXPANSION}),
}


@dataclass(frozen=True, slots=True)
class RegimeResult:
    regime: Regime
    confidence: float
    previous: Regime | None
    transitioned: bool
    reasons: tuple[str, ...]
    adx_1h: float = 0.0
    atr_pct_1h: float = 0.0


@dataclass(slots=True)
class _RegimeLatch:
    regime: str = Regime.RANGE.value
    pending: str | None = None
    pending_count: int = 0


def _f(value: Any, default: float = 0.0) -> float:
    try:
        v = float(value)
        return v if v == v else default
    except (TypeError, ValueError):
        return default


def _frame(tf: dict[str, Any], key: str) -> dict[str, Any]:
    row = tf.get(key)
    return row if isinstance(row, dict) else {}


def _lc_phase(lifecycle: Any) -> str:
    if lifecycle is None:
        return ""
    if hasattr(lifecycle, "phase"):
        ph = lifecycle.phase
        return ph.value if hasattr(ph, "value") else str(ph)
    if isinstance(lifecycle, dict):
        return str(lifecycle.get("phase") or "")
    return ""


def _lc_fall(lifecycle: Any) -> float:
    if lifecycle is None:
        return 0.0
    if hasattr(lifecycle, "fall_from_high_pct"):
        return _f(lifecycle.fall_from_high_pct)
    if isinstance(lifecycle, dict):
        return _f(lifecycle.get("fall_from_high_pct"))
    return 0.0


def _lc_bounce(lifecycle: Any) -> float:
    if lifecycle is None:
        return 0.0
    if hasattr(lifecycle, "bounce_from_low_pct"):
        return _f(lifecycle.bounce_from_low_pct)
    if isinstance(lifecycle, dict):
        return _f(lifecycle.get("bounce_from_low_pct"))
    return 0.0


def _raw_regime_vote(
    *,
    tf: dict[str, Any],
    market: dict[str, Any],
    lifecycle: Any,
    session: dict[str, Any],
) -> tuple[Regime, float, list[str]]:
    """Score-free regime vote from closed 1h + lifecycle context."""
    r1h = _frame(tf, "1h_closed") or {}
    r15 = _frame(tf, "15m_closed") or {}
    adx = _f(r1h.get("adx14"))
    atr_pct = _f(r1h.get("atr_pct"))
    squeeze_on = bool(r1h.get("squeeze_on") or r15.get("squeeze_on"))
    bb_pctile = r1h.get("bb_width_pctile")
    bb_tight = bb_pctile is not None and _f(bb_pctile) <= 0.25
    ema50 = _f(r1h.get("ema50"))
    ema200 = _f(r1h.get("ema200"))
    close_1h = _f(r1h.get("close"))
    rsi_1h = _f(r1h.get("rsi14"))
    pos = _f(session.get("pos_in_range"), 0.5)
    fall = _lc_fall(lifecycle)
    bounce = _lc_bounce(lifecycle)
    phase = _lc_phase(lifecycle)
    chg_24h = _f(market.get("chg_24h_pct") or session.get("change_24h_pct"))
    fund = _f(market.get("funding_pct"))
    reasons: list[str] = []

    if squeeze_on or bb_tight:
        reasons.append("bb_squeeze")
        if atr_pct >= 4.0 or fall >= 5.0 or bounce >= 8.0:
            reasons.append("squeeze_breaking")
            if fall >= 8.0:
                return Regime.CAPITULATION, 0.72, reasons
            if bounce >= 12.0 and pos >= 0.75:
                return Regime.EXPANSION, 0.68, reasons
            return Regime.EXPANSION, 0.62, reasons
        return Regime.SQUEEZE, 0.70, reasons

    if phase in {"exhaustion_at_high", "distribution"} and pos >= 0.80 and rsi_1h >= 68.0:
        reasons.extend([f"phase={phase}", f"rsi_1h={rsi_1h:.0f}"])
        if fund >= 0.35 or chg_24h >= 15.0:
            reasons.append("crowded_euphoria")
        return Regime.EUPHORIA, 0.74, reasons

    if phase in {"dump_active", "post_dump_bounce"} and fall >= 12.0 and bounce <= 6.0:
        reasons.extend([f"phase={phase}", f"fall={fall:.1f}%"])
        return Regime.CAPITULATION, 0.71, reasons

    if atr_pct >= 5.0 and adx >= 22.0:
        reasons.append(f"atr_expansion={atr_pct:.1f}%")
        if close_1h > 0 and ema50 > 0 and close_1h >= ema50:
            return Regime.EXPANSION, 0.65, reasons
        if close_1h > 0 and ema50 > 0 and close_1h < ema50:
            return Regime.EXPANSION, 0.63, reasons

    if adx >= 28.0 and close_1h > 0 and ema50 > 0:
        if close_1h >= ema50 and (ema200 <= 0 or ema50 >= ema200):
            reasons.append(f"adx_trend_up={adx:.0f}")
            return Regime.TREND_UP, 0.66, reasons
        if close_1h < ema50 and (ema200 <= 0 or ema50 <= ema200):
            reasons.append(f"adx_trend_down={adx:.0f}")
            return Regime.TREND_DOWN, 0.66, reasons

    if 0 < adx < 18.0:
        reasons.append(f"adx_range={adx:.0f}")
        return Regime.RANGE, 0.58, reasons

    reasons.append("neutral_fallback")
    return Regime.RANGE, 0.50, reasons


def _apply_transition(
    symbol: str,
    raw: Regime,
    *,
    state: Any,
    ticks_required: int = 2,
) -> tuple[Regime, bool]:
    store = state if state is not None else _symbol_regime_local
    sym = symbol.upper()
    regime_store = store.regime if hasattr(store, "regime") else store
    latch = regime_store.setdefault(sym, _RegimeLatch())
    if not isinstance(latch, _RegimeLatch):
        latch = _RegimeLatch(regime=str(getattr(latch, "regime", Regime.RANGE.value)))
        regime_store[sym] = latch

    try:
        prev = Regime(latch.regime)
    except ValueError:
        prev = Regime.RANGE

    if raw == prev:
        latch.pending = None
        latch.pending_count = 0
        return prev, False

    allowed = _ALLOWED_TRANSITIONS.get(prev, frozenset())
    if raw not in allowed and prev != Regime.RANGE:
        # Hold previous until a legal edge opens (range is universal hub).
        return prev, False

    if latch.pending == raw.value:
        latch.pending_count += 1
    else:
        latch.pending = raw.value
        latch.pending_count = 1

    if latch.pending_count < ticks_required:
        return prev, False

    latch.regime = raw.value
    latch.pending = None
    latch.pending_count = 0
    return raw, True


def classify_regime(
    prepared: dict[str, Any],
    lifecycle: Any,
    market: dict[str, Any],
    *,
    symbol: str = "",
    state: Any | None = None,
) -> RegimeResult:
    """Classify symbol regime with hysteresis stored in ``SymbolStateStore``."""
    tf = prepared.get("timeframes") if isinstance(prepared.get("timeframes"), dict) else {}
    session = prepared.get("session") if isinstance(prepared.get("session"), dict) else {}
    mkt = market if isinstance(market, dict) else {}

    raw, confidence, reasons = _raw_regime_vote(
        tf=tf,
        market=mkt,
        lifecycle=lifecycle,
        session=session,
    )
    store = state if state is not None else _symbol_regime_local
    sym = (symbol or str(prepared.get("symbol") or "")).upper()
    latch = store.regime.get(sym) if hasattr(store, "regime") else store.get(sym)
    try:
        previous = Regime(latch.regime) if isinstance(latch, _RegimeLatch) else None
    except (ValueError, AttributeError):
        previous = None

    regime, transitioned = _apply_transition(sym, raw, state=store)
    r1h = _frame(tf, "1h_closed") or {}
    return RegimeResult(
        regime=regime,
        confidence=round(confidence, 3),
        previous=previous,
        transitioned=transitioned,
        reasons=tuple(reasons[:6]),
        adx_1h=_f(r1h.get("adx14")),
        atr_pct_1h=_f(r1h.get("atr_pct")),
    )


def regime_conflicts_direction(regime: Regime | str, direction: Direction) -> bool:
    """C.1.2 — suppress counter-context setups when regime conflicts."""
    try:
        label = regime if isinstance(regime, Regime) else Regime(str(regime))
    except ValueError:
        return False
    d = direction.lower()
    if d == "short" and label in {Regime.TREND_UP, Regime.CAPITULATION}:
        return True
    if d == "long" and label in {Regime.TREND_DOWN, Regime.EUPHORIA}:
        return True
    if d == "long" and label == Regime.SQUEEZE:
        return True
    return False


__all__ = [
    "Regime",
    "RegimeResult",
    "classify_regime",
    "regime_conflicts_direction",
]
