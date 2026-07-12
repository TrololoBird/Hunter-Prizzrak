"""График рыночной капитализации как доп-фактор — Павел М. (`prizrak_marketcap_factor`).

Course rule (prizrak_corpus): the price chart can decouple from *истинная ценность*
because circulating supply is not constant in crypto — unlocks/locks, burns, mining
emission, or a mint back-door move the market cap independently of price. The market-cap
chart therefore reflects true value more faithfully, and Павел uses it as a *calibration*
доп-фактор — NOT an always-on gate: "в алгоритме на постоянке этого нет; смотрю, когда
сетап не вяжется". Two mechanics from the разбор:

  1. **Trend agreement.** Run the SAME structure read (`_detect_structure`, reused
     verbatim) on both the price series and the market-cap series. If the cap trend
     confirms the trade direction, that is true value agreeing with price → small bonus.
     If cap trend opposes (price pushed one way while value went the other — the classic
     low-float pump), that is a risk flag → small penalty.
  2. **Supply stability.** Levels transfer 1:1 between the cap and price charts ONLY while
     supply is stable ("на сколько процентов меняется цена, на столько же процентов
     меняется капитализация"). When the recent % change of price and cap diverge
     materially, supply is moving (a lock/unlock/burn) → the 1:1 transfer is invalid, so
     the confirm bonus is damped and the instability is surfaced as evidence.

Bounded multiplier in [0.85, 1.15] + evidence trail, exactly like ``confluence.py`` —
never a pass/fail gate. When the cap series is unavailable (no free data for the ticker,
or the factor is disabled) it returns a neutral 1.0 so the live path is untouched.
"""
from __future__ import annotations

from typing import Any, Literal

from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.prizrak.pipeline.structure import _detect_structure

_MIN_CAP_POINTS = 20


def _cap_bars(cap_series: list[list[float]]) -> list[dict[str, float]]:
    """CoinGecko ``market_chart`` returns ``[[ts_ms, market_cap], ...]`` — a line, no
    OHLC. Павел reads the cap *line*; the swing-pivot structure detector only needs
    high/low/close, so synthesise flat bars (open=high=low=close=cap value). A wick can't
    exist on a line, which is fine — pivots are then pure turning points of the value."""
    bars: list[dict[str, float]] = []
    for point in cap_series:
        if not point or len(point) < 2 or point[1] is None:
            continue
        cap = float(point[1])
        bars.append({"open": cap, "high": cap, "low": cap, "close": cap, "volume": 0.0})
    return bars


def _trend(struct: dict[str, Any]) -> Literal["bull", "bear", "neutral"]:
    """Reduce a ``_detect_structure`` result to a single directional trend — same rule as
    ``orchestrator._tier_trend`` (kept local to avoid a cross-module import cycle)."""
    if not struct:
        return "neutral"
    bull = bool(struct.get("hh") or struct.get("hl") or struct.get("bos_up") or struct.get("choch_bull"))
    bear = bool(struct.get("lh") or struct.get("ll") or struct.get("bos_down") or struct.get("choch_bear"))
    if bull and not bear:
        return "bull"
    if bear and not bull:
        return "bear"
    return "neutral"


def _pct_change(closes: list[float], *, window: int) -> float | None:
    """Recent % change over the last ``window`` samples (relative to the earlier point)."""
    if len(closes) < window + 1:
        return None
    old = closes[-window - 1]
    new = closes[-1]
    if old == 0:
        return None
    return (new - old) / abs(old)


def compute_marketcap_factor(
    price_ohlcv: list[list[float]],
    cap_series: list[list[float]] | None,
    *,
    direction: str,
    cfg: PrizrakConfig,
) -> dict[str, Any]:
    """Bounded market-cap доп-фактор multiplier in [0.85, 1.15] + evidence. Never gates.

    Args:
        price_ohlcv: raw CCXT rows ``[ts, o, h, l, c, v]`` for the traded asset.
        cap_series: CoinGecko ``market_chart`` points ``[[ts_ms, market_cap], ...]`` or
            ``None`` when unavailable / the factor is disabled.
        direction: ``"long"`` or ``"short"`` — the candidate trade side.
        cfg: prizrak config (supplies the same structure-detection lookbacks used
            everywhere else, plus the factor's own bonus/penalty knobs).

    Returns:
        ``{"multiplier": float, "evidence": [...], "cap_trend": str, "price_trend": str,
        "supply": str}``. Multiplier is a bounded, non-gating strength factor.
    """
    if not cfg.marketcap_enabled:
        return {"multiplier": 1.0, "evidence": ["marketcap_disabled"]}
    if not cap_series or len(cap_series) < _MIN_CAP_POINTS:
        return {"multiplier": 1.0, "evidence": ["marketcap_unavailable"]}

    cap_bars = _cap_bars(cap_series)
    if len(cap_bars) < _MIN_CAP_POINTS:
        return {"multiplier": 1.0, "evidence": ["marketcap_unavailable"]}

    price_bars = [
        {"open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5] if len(r) > 5 else 0.0}
        for r in price_ohlcv
        if r and len(r) >= 5
    ]
    if len(price_bars) < 4:
        return {"multiplier": 1.0, "evidence": ["insufficient_price_bars"]}

    cap_struct = _detect_structure(
        cap_bars,
        lookback_pivot=cfg.structure_lookback_pivot,
        lookback_hh_ll=cfg.structure_lookback_hh_ll,
        bos_buffer=cfg.structure_bos_buffer_pct,
    )
    price_struct = _detect_structure(
        price_bars,
        lookback_pivot=cfg.structure_lookback_pivot,
        lookback_hh_ll=cfg.structure_lookback_hh_ll,
        bos_buffer=cfg.structure_bos_buffer_pct,
    )
    cap_trend = _trend(cap_struct)
    price_trend = _trend(price_struct)

    # Supply stability: compare recent % change of price vs cap. Near-equal ⇒ supply
    # stable, 1:1 level transfer valid. Divergent ⇒ supply is moving (lock/unlock/burn)
    # ⇒ the transfer is invalid and the confirm bonus is damped.
    window = min(cfg.structure_lookback_hh_ll, len(cap_bars) - 1, len(price_bars) - 1)
    cap_closes = [b["close"] for b in cap_bars]
    price_closes = [b["close"] for b in price_bars]
    cap_pct = _pct_change(cap_closes, window=window)
    price_pct = _pct_change(price_closes, window=window)

    evidence: list[str] = []
    supply = "unknown"
    supply_damp = 1.0
    if cap_pct is not None and price_pct is not None:
        drift = abs(price_pct - cap_pct)
        if drift <= cfg.marketcap_supply_drift_pct:
            supply = "stable"
            evidence.append(f"supply_stable(Δ{drift * 100:.1f}%)")
        else:
            supply = "unstable"
            supply_damp = 0.5  # supply moving ⇒ true value decoupled from price; halve confirm weight
            evidence.append(f"supply_unstable(price{price_pct * 100:+.1f}%_vs_cap{cap_pct * 100:+.1f}%)")

    want: Literal["bull", "bear"] = "bull" if direction == "long" else "bear"
    opposite: Literal["bull", "bear"] = "bear" if want == "bull" else "bull"

    mult = 1.0
    if cap_trend == want:
        bonus = cfg.marketcap_confirm_bonus * supply_damp
        mult += bonus
        evidence.append(f"marketcap_confirms_{want}(+{bonus:.2f})")
    elif cap_trend == opposite:
        # Value moving against the trade — the low-float-pump risk Павел warns about.
        # A divergence penalty is NOT damped by supply instability: instability is the
        # very mechanism that makes an opposing cap trend dangerous.
        mult -= cfg.marketcap_diverge_penalty
        evidence.append(f"marketcap_diverges_{cap_trend}(-{cfg.marketcap_diverge_penalty:.2f})")
    else:
        evidence.append("marketcap_neutral")

    mult = max(0.85, min(1.15, mult))
    return {
        "multiplier": round(mult, 3),
        "evidence": evidence,
        "cap_trend": cap_trend,
        "price_trend": price_trend,
        "supply": supply,
    }


__all__ = ["compute_marketcap_factor"]
