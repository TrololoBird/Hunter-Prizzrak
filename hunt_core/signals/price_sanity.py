"""Independent price sanity — withhold signals on implausible quotes (plan §5.13)."""
from __future__ import annotations

from typing import Any


def price_sanity_check(row: dict[str, Any], *, max_deviation_pct: float = 25.0) -> tuple[bool, str]:
    """Return (ok, reason). Compares live price to structural / session reference."""
    try:
        price = float(row.get("price") or 0)
    except (TypeError, ValueError):
        return False, "missing_price"
    if price <= 0:
        return False, "missing_price"

    refs: list[float] = []
    market = row.get("market") if isinstance(row.get("market"), dict) else {}
    for key in ("mark_price", "index_price", "last_close_1h", "map_vp_poc"):
        try:
            v = float(market.get(key) or 0)
            if v > 0:
                refs.append(v)
        except (TypeError, ValueError):
            continue

    structure = row.get("structure") if isinstance(row.get("structure"), dict) else {}
    kl = structure.get("key_levels") if isinstance(structure.get("key_levels"), dict) else {}
    for key in ("support", "resistance", "last_swing_high", "last_swing_low"):
        try:
            v = float(kl.get(key) or 0)
            if v > 0:
                refs.append(v)
        except (TypeError, ValueError):
            continue

    session = row.get("session_meta") if isinstance(row.get("session_meta"), dict) else {}
    for key in ("price_high", "price_low", "last_price"):
        try:
            v = float(session.get(key) or 0)
            if v > 0:
                refs.append(v)
        except (TypeError, ValueError):
            continue

    if not refs:
        return True, ""

    ref = sum(refs) / len(refs)
    dev = abs(price - ref) / ref * 100.0
    if dev > max_deviation_pct:
        return False, f"price_deviation_{dev:.1f}pct"
    return True, ""
