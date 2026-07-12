"""bias ↔ liquidation/DOM reconciliation as a bounded confluence доп-фактор + risk flag.

The Prizrak decision (`_htf_bias`, `build_prizrak_signals`) is purely structural — it reads
OHLCV multi-scale structure and never looks at the bot's OWN liquidation map or order-book
(DOM) imbalance. The ETH разбор (`research/prizrak_corpus/prizrak_eth.razbor.md`) is the
authority for why that is a bug: the bot printed a confident structural **SHORT** while its
own liq map said **short-squeeze ↑1818** and DOM showed **buyers +0.222** — and the squeeze/
buyers were right (ETH rose). This module reconciles the candidate's structural direction
against those two real signals:

- **liquidation cascade** (`liq_cascade_risk`): ``short_squeeze`` = upward pressure (bullish),
  ``long_flush`` = downward pressure (bearish). Trusted **only when non-synthetic**
  (``liq_synthetic_only`` is False, i.e. realized events exist) — a leverage-tier *estimate*
  must not veto structure.
- **DOM imbalance** (`map_book_imbalance_1pct`, +buyers / −sellers): real order-book data,
  trusted whenever it exceeds the neutral band.

When the structural direction **contradicts** the combined market pressure it returns a
bounded penalty multiplier and ``conflict=True`` (surfaced as a risk flag); when it agrees it
returns a small bonus. Bounded to ``[0.85, 1.15]`` and **non-gating** — it never vetoes or
flips the candidate, only down-weights and warns. Neutral (1.0) when disabled or without data,
so callers with no map context (tests, cold rows) are unaffected.
"""
from __future__ import annotations

from typing import Any, Mapping

from hunt_core.prizrak.config import PrizrakConfig

# Max strength adjustment; matches the dominance factor's ±0.15 envelope.
_MAX_PENALTY = 0.15
_MAX_BONUS = 0.10
# DOM contributes half a "unit" of pressure vs a full unit from a realized cascade — a book
# snapshot is a weaker directional tell than an actual liquidation cascade in progress.
_DOM_WEIGHT = 0.5
_CASCADE_WEIGHT = 1.0


def compute_liquidation_factor(
    liq_ctx: Mapping[str, Any] | None,
    *,
    direction: str,
    cfg: PrizrakConfig | None = None,
) -> dict[str, Any]:
    """Bounded multiplier reconciling structural ``direction`` against liq cascade + DOM.

    Args:
        liq_ctx: Per-tick market keys — ``liq_cascade_risk`` (``"short_squeeze"``/
            ``"long_flush"``/``None``), ``liq_synthetic_only`` (bool), and
            ``map_book_imbalance_1pct`` (float, +buyers/−sellers). ``None`` → neutral.
        direction: Candidate direction, ``"long"`` or ``"short"``.
        cfg: Prizrak config (band + enable flag).

    Returns:
        ``{"multiplier": float, "evidence": [str, ...], "conflict": bool}``.
    """
    cfg = cfg or PrizrakConfig.load()
    if not cfg.liq_reconcile_enabled or not liq_ctx:
        return {"multiplier": 1.0, "evidence": ["liq_disabled"], "conflict": False}

    dir_sign = 1.0 if direction == "long" else -1.0
    cascade = liq_ctx.get("liq_cascade_risk")
    synthetic_only = bool(liq_ctx.get("liq_synthetic_only"))
    imb = liq_ctx.get("map_book_imbalance_1pct")

    market = 0.0  # >0 = upward/bullish pressure, <0 = downward/bearish
    evidence: list[str] = []

    # Liquidation cascade — realized data only (a synthetic leverage-tier estimate must not
    # drive the conflict flag; it may still hint via DOM below).
    cascade_realized = bool(cascade) and not synthetic_only
    if cascade_realized:
        if cascade == "short_squeeze":
            market += _CASCADE_WEIGHT
            evidence.append("liq:шорт-сквиз↑")
        elif cascade == "long_flush":
            market -= _CASCADE_WEIGHT
            evidence.append("liq:лонг-флаш↓")

    # DOM book imbalance — real order-book data, trusted outside the neutral band.
    dom_strong = False
    if isinstance(imb, (int, float)) and abs(imb) >= cfg.liq_dom_neutral_band:
        d = 1.0 if imb > 0 else -1.0
        market += d * _DOM_WEIGHT
        dom_strong = abs(imb) >= 2.0 * cfg.liq_dom_neutral_band
        evidence.append(f"DOM:{'покупатели' if d > 0 else 'продавцы'}({imb:+.2f})")

    if market == 0.0:
        return {"multiplier": 1.0, "evidence": evidence or ["liq_neutral"], "conflict": False}

    align = dir_sign * market  # >0 aligned, <0 contradiction
    strength = min(1.0, abs(market) / (_CASCADE_WEIGHT + _DOM_WEIGHT))

    if align < 0:
        mult = 1.0 - _MAX_PENALTY * strength
        # A conflict is "hard" (risk flag) only when backed by real data: a realized cascade,
        # or a strong DOM imbalance. A weak DOM-only lean down-weights but does not flag.
        conflict = cascade_realized or dom_strong
    else:
        mult = 1.0 + _MAX_BONUS * strength
        conflict = False

    return {
        "multiplier": round(max(0.85, min(1.15, mult)), 4),
        "evidence": evidence,
        "conflict": bool(conflict),
    }


__all__ = ["compute_liquidation_factor"]
