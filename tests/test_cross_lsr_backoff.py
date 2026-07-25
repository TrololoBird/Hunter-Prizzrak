"""Cross-venue long/short poll must not re-hit an endpoint that cannot serve the symbol.

``has.fetchLongShortRatioHistory`` is a VENUE capability, not a per-symbol one. Measured live on
Bitget: BTC/ETH/SOL return 30 rows, while XRP, AVAX, XAU, XAG and PAXG all error with
«The data fetched by <SYM> is empty». The data path was already correct (a poller ``None`` is
skipped, never stored as a fabricated ratio), but every uncovered symbol re-hit the endpoint on
every cycle forever — burning rate-limit on a call that cannot succeed and emitting one warning per
symbol per cycle, which is what buries the failures that DO matter.
"""
from __future__ import annotations

from hunt_core.engine.multi import _LSR_BLANK_RETRY_S, MultiEngine


def _engine() -> MultiEngine:
    """A MultiEngine without touching the network: only the back-off bookkeeping is exercised."""
    eng = MultiEngine.__new__(MultiEngine)
    eng._lsr_blank = {"bitget": {}}  # type: ignore[attr-defined]
    return eng


def test_symbol_is_due_until_it_comes_back_empty() -> None:
    eng = _engine()
    assert eng._lsr_due("bitget", "XRP/USDT:USDT") is True


def test_empty_history_puts_the_symbol_in_backoff() -> None:
    """★ After an empty response the pair is skipped — no per-cycle retry, no per-cycle warning."""
    import time as _time

    eng = _engine()
    eng._lsr_blank["bitget"]["XRP/USDT:USDT"] = _time.monotonic() + _LSR_BLANK_RETRY_S
    assert eng._lsr_due("bitget", "XRP/USDT:USDT") is False
    assert eng._lsr_due("bitget", "BTC/USDT:USDT") is True, "a covered symbol must stay unaffected"


def test_backoff_expires_so_added_coverage_is_picked_up() -> None:
    """The skip is a back-off, not a permanent blacklist — a venue may extend coverage."""
    import time as _time

    eng = _engine()
    eng._lsr_blank["bitget"]["XRP/USDT:USDT"] = _time.monotonic() - 1.0  # deadline already passed
    assert eng._lsr_due("bitget", "XRP/USDT:USDT") is True
    assert 0 < _LSR_BLANK_RETRY_S <= 24 * 3600, "re-probe within a day, without a restart"


def test_unknown_venue_is_due_rather_than_crashing() -> None:
    eng = _engine()
    assert eng._lsr_due("okx", "BTC/USDT:USDT") is True
