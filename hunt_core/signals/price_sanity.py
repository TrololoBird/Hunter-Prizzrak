"""Independent price sanity — withhold signals on implausible quotes (plan §5.13)."""
from __future__ import annotations

from collections.abc import Sequence

import structlog

LOG = structlog.get_logger(__name__)


def price_sanity_check(
    price: float,
    *,
    refs: Sequence[float] = (),
    max_deviation_pct: float = 25.0,
) -> tuple[bool, str]:
    """Return ``(ok, reason)`` — compare the live ``price`` to independent structural references.

    ADR-0004 Phase 9: takes the typed ``price`` (``MarketView.last_price``) plus an explicit list of
    reference levels the caller derives from typed handles (the deep lane passes the VP POC —
    ``MapBundle.volume_profile.primary_poc`` — the only anchor that ever resolved on this path;
    the legacy ``mark_price``/``index_price``/``last_close_1h`` reads were phantom keys). No refs →
    the quote is accepted (fail-loud: never fabricate a reference to reject on).

    Args:
        price: Live price under test.
        refs: Independent positive reference levels; their mean is the deviation baseline.
        max_deviation_pct: Reject when ``|price/ref − 1| × 100`` exceeds this.

    Returns:
        ``(True, "")`` when in range or no refs; ``(False, reason)`` on a missing price or a
        deviation past the bound.
    """
    if price <= 0:
        return False, "missing_price"

    valid_refs = [float(r) for r in refs if isinstance(r, (int, float)) and float(r) > 0]
    if not valid_refs:
        return True, ""

    ref = sum(valid_refs) / len(valid_refs)
    dev = abs(price - ref) / ref * 100.0
    if dev > max_deviation_pct:
        return False, f"price_deviation_{dev:.1f}pct"
    return True, ""
