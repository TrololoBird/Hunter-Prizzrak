"""Shared playbook checklist — single source for fusion rank and delivery gates."""
from __future__ import annotations


# Required checks per archetype (named standards, not fuel weights).
PLAYBOOK_REQUIRED: dict[str, frozenset[str]] = {
    "predump_short": frozenset(
        {
            "distribution_phase",
            "pos_near_high",
            "oi_distribution",
            "bear_cvd_div",
            "sweep_reclaim",
        }
    ),
    "prepump_long": frozenset(
        {
            "coil_phase",
            "vp_accumulation",
            "va_contraction",
            "bid_absorption",
            "bull_cvd_div",
        }
    ),
    "ignition_long": frozenset(
        {
            "neg_funding",
            "short_liq_above",
            "squeeze_regime",
            "cvd_absorption",
            "obi_bid",
        }
    ),
}

PLAYBOOK_N_OF_M: dict[str, tuple[int, int]] = {
    "predump_short": (4, 5),
    "prepump_long": (4, 5),
    # was (5,7) before removing lagging vah_break_5m/vol_above_median_5m
    "ignition_long": (5, 5),
}


def best_archetype_by_ratio(checks: dict[str, bool]) -> tuple[str, float, int, int]:
    """Pick archetype with highest pass ratio."""
    best_arch = "none"
    best_ratio = 0.0
    best_pc = 0
    best_req = 0
    for arch in ("predump_short", "prepump_long", "ignition_long"):
        keys = PLAYBOOK_REQUIRED.get(arch, frozenset())
        if not keys:
            continue
        pc = sum(1 for k in keys if checks.get(k))
        total = len(keys)
        ratio = (100.0 * pc / total) if total > 0 else 0.0
        min_pass, _ = PLAYBOOK_N_OF_M.get(arch, (total, total))
        if ratio > best_ratio or (ratio == best_ratio and pc > best_pc):
            best_arch = arch
            best_ratio = ratio
            best_pc = pc
            best_req = min_pass
    if best_pc <= 0:
        return "none", 0.0, 0, 0
    return best_arch, round(best_ratio, 1), best_pc, best_req


__all__ = [
    "PLAYBOOK_N_OF_M",
    "PLAYBOOK_REQUIRED",
    "best_archetype_by_ratio",
]
