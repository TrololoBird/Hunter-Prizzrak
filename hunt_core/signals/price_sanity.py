"""Independent price sanity — withhold signals on implausible quotes (plan §5.13)."""
from __future__ import annotations

import structlog

from typing import Any

LOG = structlog.get_logger(__name__)
def price_sanity_check(row: dict[str, Any], *, max_deviation_pct: float = 25.0) -> tuple[bool, str]:
    """Return (ok, reason). Compares live price to structural / session reference."""
    try:
        price = float(row.get("price") or 0)
    except (TypeError, ValueError):
        LOG.debug("price_sanity_check row.price float conversion failed", exc_info=True)
        return False, "missing_price"
    if price <= 0:
        return False, "missing_price"

    refs: list[float] = []
    market_raw = row.get("market")
    market: dict[str, Any] = market_raw if isinstance(market_raw, dict) else {}
    for key in ("mark_price", "index_price", "last_close_1h", "map_vp_poc"):
        try:
            v = float(market.get(key) or 0)
            if v > 0:
                refs.append(v)
        except (TypeError, ValueError):
            LOG.debug("price_sanity_check market.%s float conversion failed", key, exc_info=True)
            continue

    structure_raw = row.get("structure")
    structure: dict[str, Any] = structure_raw if isinstance(structure_raw, dict) else {}
    kl_raw = structure.get("key_levels")
    kl: dict[str, Any] = kl_raw if isinstance(kl_raw, dict) else {}
    for key in ("support", "resistance", "last_swing_high", "last_swing_low"):
        try:
            v = float(kl.get(key) or 0)
            if v > 0:
                refs.append(v)
        except (TypeError, ValueError):
            LOG.debug("price_sanity_check structure.key_levels.%s float conversion failed", key, exc_info=True)
            continue

    session_raw = row.get("session_meta")
    session: dict[str, Any] = session_raw if isinstance(session_raw, dict) else {}
    for key in ("price_high", "price_low", "last_price"):
        try:
            v = float(session.get(key) or 0)
            if v > 0:
                refs.append(v)
        except (TypeError, ValueError):
            LOG.debug("price_sanity_check session_meta.%s float conversion failed", key, exc_info=True)
            continue

    if not refs:
        return True, ""

    ref = sum(refs) / len(refs)
    dev = abs(price - ref) / ref * 100.0
    if dev > max_deviation_pct:
        return False, f"price_deviation_{dev:.1f}pct"
    return True, ""
