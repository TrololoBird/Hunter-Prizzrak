"""Per-symbol TTL blacklist for persistent fetch errors (delisted / halted).

Moved from ``runtime.state`` to ``data`` (data-plane concern).
TTL: auto-remove after BLACKLIST_TTL_S seconds.
"""
from __future__ import annotations

import time

_symbol_blacklist: dict[str, float] = {}
BLACKLIST_TTL_S = 3600  # 1 hour


def blacklist_symbol(symbol: str) -> None:
    sym = symbol.upper()
    _symbol_blacklist[sym] = time.monotonic() + BLACKLIST_TTL_S


def unblacklist_symbol(symbol: str) -> None:
    _symbol_blacklist.pop(symbol.upper(), None)


def is_blacklisted(symbol: str) -> bool:
    sym = symbol.upper()
    expiry = _symbol_blacklist.get(sym)
    if expiry is None:
        return False
    if time.monotonic() >= expiry:
        _symbol_blacklist.pop(sym, None)
        return False
    return True


def blacklisted_symbols() -> frozenset[str]:
    now = time.monotonic()
    expired = [s for s, exp in _symbol_blacklist.items() if now >= exp]
    for s in expired:
        _symbol_blacklist.pop(s, None)
    return frozenset(_symbol_blacklist.keys())


__all__ = [
    "BLACKLIST_TTL_S",
    "blacklist_symbol",
    "blacklisted_symbols",
    "is_blacklisted",
    "unblacklist_symbol",
]
