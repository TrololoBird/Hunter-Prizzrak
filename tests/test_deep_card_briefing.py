"""The deep card must state its conclusion, not leave it to be reconstructed.

Built against the real BTC card from 2026-07-16, which opened on МТФ mechanics and
spread ~40 numbers over seven sections without ever saying: what state are we in, what
regime is this, and how far is the nearest thing to act on. All three were derivable —
that is exactly the problem, since deriving them was left to the reader.
"""

from __future__ import annotations

from typing import Any

from hunt_core.prizrak.format_telegram import _briefing_text, _nearest_zone


class _Report:
    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row
        self.symbol = "BTCUSDT"


# The live card's own numbers.
_PRICE = 63894.9


def _row(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "price": _PRICE,
        "prizrak_summary": {"action": "wait"},
        "prizrak_structure": {
            "htf_bias": {"bias": "neutral", "score": 0.0, "regime": "accumulation"}
        },
        "prizrak_interest_zones": {
            "tf": "1h",
            "long": {"lo": 62446.6, "hi": 62743.7, "touches": 4},
            "short": {"lo": 64549.8, "hi": 65037.8, "touches": 11},
        },
    }
    base.update(over)
    return base


def test_nearest_zone_picks_the_closer_side() -> None:
    """Short edge 64549.8 is ~1.0% away; long edge 62743.7 is ~1.8%. Short wins."""
    got = _nearest_zone(_row(), _PRICE)
    assert got is not None
    side, edge, dist, _limit_ok = got
    assert side == "short"
    assert edge == 64549.8
    assert 1.0 <= dist <= 1.1


def test_price_inside_a_zone_reports_zero_distance() -> None:
    got = _nearest_zone(_row(), 62500.0)
    assert got is not None
    side, _edge, dist, _limit_ok = got
    assert side == "long"
    assert dist == 0.0


def test_nearest_zone_treats_an_unknown_verdict_as_no_limit() -> None:
    """I-6: a zone whose producer never ruled on limit_ok is not a licence to limit.

    The gate lives in compute_interest_zones; any other producer (an old cached row, a
    test fixture, a future caller) yields zones without the flag. Defaulting those to
    "limit is fine" would reintroduce стр.31 through the back door on exactly the rows
    we know least about.
    """
    got = _nearest_zone(_row(), _PRICE)  # fixture zones carry no limit_ok
    assert got is not None
    assert got[3] == "unknown"


def test_a_sawn_level_gets_the_saw_remedy_not_the_worked_one() -> None:
    """стр.28 сц.7 and стр.31 forbid the limit for DIFFERENT reasons, so they prescribe
    different next steps: a sawn level is waited out, a worked one needs слом on МТФ.
    Collapsing them to one bool printed «только по слому МТФ» over a пила."""
    row = _row(
        prizrak_interest_zones={
            "tf": "1h",
            "short": {"lo": 64549.8, "hi": 65037.8, "touches": 8, "worked": 0,
                      "saw": True, "limit_ok": False},
        }
    )
    out = _briefing_text(_Report(row), _PRICE)
    assert out is not None
    assert "пила" in out
    assert "слому МТФ" not in out, "wrong remedy — that is the стр.31 rule, not стр.28"


def test_wait_briefing_does_not_advertise_limits_on_a_worked_level() -> None:
    """стр.31: «уровень лимитными ордерами больше не торгуем… только по факту слома».

    The WAIT headline hardcoded «работают лимит-зоны», so the card advertised limits on
    every level including the ones the course takes limits off — and the touches-primary
    ranking means the zone shown is the MOST worked one available.
    """
    row = _row(
        prizrak_interest_zones={
            "tf": "1h",
            "short": {"lo": 64549.8, "hi": 65037.8, "touches": 11, "worked": 2, "limit_ok": False},
        }
    )
    out = _briefing_text(_Report(row), _PRICE)
    assert out is not None
    assert "работают лимит-зоны" not in out
    assert "лимит НЕ ставим" in out


def test_wait_briefing_still_advertises_limits_on_a_clean_level() -> None:
    """The gate must not swallow the honest case — an untested base IS a limit zone."""
    row = _row(
        prizrak_interest_zones={
            "tf": "1h",
            "short": {"lo": 64549.8, "hi": 65037.8, "touches": 5, "worked": 0, "limit_ok": True},
        }
    )
    out = _briefing_text(_Report(row), _PRICE)
    assert out is not None
    assert "работают лимит-зоны" in out


def test_briefing_states_wait_regime_and_distance() -> None:
    out = _briefing_text(_Report(_row()), _PRICE)
    assert out is not None
    assert "ЖДЁМ" in out, "WAIT must be stated, not implied"
    # The accumulation verdict is the most actionable read the МТФ block produces and
    # used to be reported as plain 'neutral/undetermined'.
    assert "накопление" in out
    assert "шорт против набора" in out
    # And the distance the reader would otherwise compute by hand.
    assert "1.0% от цены" in out
    assert "64549.8" in out


def test_briefing_leads_with_the_action_when_a_setup_is_live() -> None:
    out = _briefing_text(_Report(_row(prizrak_summary={"action": "long"})), _PRICE)
    assert out is not None
    assert out.splitlines()[0].startswith("<b>🟢 ЛОНГ</b>")


def test_briefing_says_price_is_in_the_zone() -> None:
    out = _briefing_text(_Report(_row()), 62500.0)
    assert out is not None
    # Prepositional case: «в зонЕ». The old assertion accepted «цена В ЛОНГ-ЗОНА» — and
    # its `or "цена В" in out` escape hatch would have passed on any wording at all.
    assert "цена В ЛОНГ-ЗОНЕ" in out


def test_distribution_regime_is_named_distinctly() -> None:
    row = _row(
        prizrak_structure={"htf_bias": {"bias": "neutral", "regime": "distribution"}}
    )
    out = _briefing_text(_Report(row), _PRICE)
    assert out is not None
    assert "распределение" in out
    assert "накопление" not in out


def test_briefing_survives_a_row_with_no_zones_or_regime() -> None:
    """Never raise on a thin row — the card must still render."""
    out = _briefing_text(_Report({"price": _PRICE, "prizrak_summary": {}}), _PRICE)
    assert out is not None
    assert "ЖДЁМ" in out


def test_no_zone_line_when_zone_prices_are_junk() -> None:
    row = _row(prizrak_interest_zones={"long": {"lo": 0, "hi": 0}, "short": None})
    assert _nearest_zone(row, _PRICE) is None


def test_live_setup_shows_the_SETUP_entry_not_a_pending_limit_zone() -> None:
    """The briefing must not contradict the card one line below it.

    Live: «🔴 ШОРТ — сетап активен» followed by «ближайшая шорт-зона: 81.2800 — 7.4% от
    цены», while the setup's actual entry sat at 75.19–75.49. Two different objects, the
    same word ("short"), contradictory numbers, one line apart — read as a single
    thought. The briefing exists so the reader does not have to reconcile the card
    against itself, so when a setup is live it must speak about THAT setup's entry.
    """
    row = _row(
        prizrak_summary={"action": "short", "entry_lo": 75.1893, "entry_hi": 75.4907},
        prizrak_interest_zones={"short": {"lo": 81.28, "hi": 82.0, "touches": 4}},
    )
    out = _briefing_text(_Report(row), 75.70)
    assert out is not None
    assert "81.28" not in out, "must not advertise a pending limit zone as the setup"
    assert "75.4907" in out or "75.49" in out
    assert "вход:" in out


def test_live_setup_says_when_price_is_inside_the_entry_band() -> None:
    row = _row(prizrak_summary={"action": "long", "entry_lo": 75.0, "entry_hi": 75.5})
    out = _briefing_text(_Report(row), 75.2)
    assert out is not None
    assert "цена В ЗОНЕ ВХОДА" in out


def test_live_setup_without_an_entry_band_still_renders() -> None:
    row = _row(prizrak_summary={"action": "short"})
    out = _briefing_text(_Report(row), 75.70)
    assert out is not None
    assert "ШОРТ" in out
