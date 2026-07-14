"""The [hunter] TOML section actually reaches hunter_thresholds() (was doc-only).

Regression pin for the config-wiring fix: the translation used to emit the section
under key "scanner" (which nothing reads) with only 4 renamed keys, so every other
[hunter] key was silently ignored and the effective value was the inline fallback.
Now the whole section is forwarded under "hunter"; editing the TOML takes effect.
"""
from __future__ import annotations

from hunt_core.domain.config import universal_section_from_defaults
from hunt_core.params.store import hunter_thresholds, universal_section


def test_full_hunter_section_is_forwarded() -> None:
    d = universal_section_from_defaults("hunter")
    # Keys that used to be dropped by the 4-key translation must now be present.
    for k in (
        "min_quote_volume_usd", "min_open_interest_usd", "min_listing_age_days",
        "max_recent_volatility_pct", "min_change_pct_for_hot", "max_hot_coins",
        "score_watch", "score_priority", "scan_interval_s", "watchlist_limit",
    ):
        assert k in d, f"[hunter].{k} not forwarded — still doc-only"


def test_hunter_thresholds_reflect_toml_not_just_fallbacks() -> None:
    us = universal_section("hunter")
    # These come from the TOML section, not the inline .get() fallback.
    assert us.get("scan_interval_s") == 900
    assert us.get("watchlist_limit") == 50
    assert us.get("min_quote_volume_usd") == 10_000_000
    ht = hunter_thresholds()
    assert ht["scan_interval_s"] == 900
    assert ht["watchlist_limit"] == 50
    assert ht["min_quote_volume_usd"] == 10_000_000.0
