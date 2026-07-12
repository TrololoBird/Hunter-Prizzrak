"""Stop sits BEHIND the structure boundary with a buffer, not flat off the entry.

Course с. 33: «Безопасный СТОП за дно структуры с запасом 1-3%. Это вас обезопасит от
сквизов». с. 19: «СТОП прятать с запасом за структуру (границы) 1-5%». A stop placed a
flat distance below the *entry* sits INSIDE the structure when the entry is above the
zone low, so a wick/squeeze into the base takes it out — the reason stops were getting
knocked. Anchoring below the zone LOW (long) / above the zone HIGH (short) fixes that.
"""

from __future__ import annotations

from hunt_core.prizrak.orchestrator import _structural_stop

BUF = 0.02
ZONE = {"lo": 100.0, "hi": 104.0}


def test_long_stop_is_below_the_zone_low_not_the_entry() -> None:
    """Entry at the ПОК (102), inside the zone — stop must sit below the zone LOW (100)."""
    stop = _structural_stop("long", entry=102.0, zone=ZONE, buffer_pct=BUF)
    assert stop == 100.0 * (1 - BUF)          # 98.0, behind the structure
    assert stop < ZONE["lo"]                  # below the base, squeeze-safe
    # A flat 2% off the entry would sit at 99.96 — INSIDE the 100–104 base, easily wicked.
    assert stop < 102.0 * (1 - BUF)


def test_short_stop_is_above_the_zone_high() -> None:
    stop = _structural_stop("short", entry=102.0, zone=ZONE, buffer_pct=BUF)
    assert stop == 104.0 * (1 + BUF)          # 106.08, behind the structure top
    assert stop > ZONE["hi"]


def test_boundary_entry_coincides_with_structural_stop() -> None:
    """Buying exactly at the zone low: structural and entry-anchored stops coincide."""
    stop = _structural_stop("long", entry=100.0, zone=ZONE, buffer_pct=BUF)
    assert stop == 100.0 * (1 - BUF)


def test_buffer_widens_the_stop() -> None:
    tight = _structural_stop("long", entry=102.0, zone=ZONE, buffer_pct=0.01)
    wide = _structural_stop("long", entry=102.0, zone=ZONE, buffer_pct=0.05)
    assert wide < tight < ZONE["lo"]          # bigger buffer → stop further below the base


def test_falls_back_to_entry_buffer_without_a_usable_zone() -> None:
    assert _structural_stop("long", entry=102.0, zone=None, buffer_pct=BUF) == 102.0 * (1 - BUF)
    assert _structural_stop("long", entry=102.0, zone={}, buffer_pct=BUF) == 102.0 * (1 - BUF)
