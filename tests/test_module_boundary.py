"""The two strategies are independent, and the backtest covers only ONE of them.

This file exists because of a REPEATED agent failure, not a hypothetical one. The reflex is:
"I changed target geometry ⇒ that is an emission change ⇒ run the backtest gate." The words
fit both modules, so the reflex fires on `hunt_core/prizrak/` too — and the harness there
executes none of the edited code. The run returns the same number, and that number reads as
"no regression" while being no measurement at all. False safety is worse than no safety: it
launders an unmeasured change as a measured one.

Prose in CLAUDE.md and the skill can be skimmed. A failing test cannot. So the boundary the
docs assert is pinned here mechanically:

  * no `research/backtest_*.py` may import `hunt_core.prizrak` — if one ever does, this fails
    and whoever wired it up must revisit the scope claims in CLAUDE.md and the backtest-gate
    skill, both of which say "no backtest imports prizrak" as a load-bearing FACT;
  * every `research/backtest_*.py` must import the manipulations path — that is what makes it
    a manipulations harness;
  * the two modules must not import each other.

See CLAUDE.md § «Два модуля — НЕ ПУТАТЬ» and .claude/skills/backtest-gate/SKILL.md.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_RESEARCH = _ROOT / "research"
_PRIZRAK = _ROOT / "hunt_core" / "prizrak"
_SCANNER = _ROOT / "hunt_core" / "scanner"

# Substrings that mean "this file reaches into the manipulations detection/delivery path".
_MANIP_MARKERS = ("advance_manipulation_scales", "manipulation_delivery")


def _backtests() -> list[Path]:
    return sorted(_RESEARCH.glob("backtest_*.py"))


def _imports_module(path: Path, dotted: str) -> bool:
    """True if `path` imports `dotted` — matched on import statements, not any mention.

    A bare substring search would count a docstring or a comment saying "does not import
    hunt_core.prizrak" as an import, which is exactly the kind of false positive that makes
    a guard test get deleted instead of fixed.
    """
    src = path.read_text(encoding="utf-8")
    pattern = rf"^\s*(?:from\s+{re.escape(dotted)}[.\s]|import\s+{re.escape(dotted)}\b)"
    return re.search(pattern, src, re.MULTILINE) is not None


def test_there_are_backtests_to_check() -> None:
    """Guard the guard: if the glob silently matches nothing, every test below passes
    vacuously and the boundary is unprotected while looking green."""
    assert _backtests(), "no research/backtest_*.py found — this file is testing nothing"


@pytest.mark.parametrize("path", _backtests(), ids=lambda p: p.name)
def test_no_backtest_imports_prizrak(path: Path) -> None:
    """THE pin. CLAUDE.md and the backtest-gate skill both state, as the reason prizrak
    changes must be measured on live data instead, that no backtest imports prizrak."""
    assert not _imports_module(path, "hunt_core.prizrak"), (
        f"{path.name} now imports hunt_core.prizrak. The scope claims in CLAUDE.md "
        "(§ Два модуля) and .claude/skills/backtest-gate/SKILL.md say no backtest does — "
        "they are now WRONG and must be updated together with this test."
    )


@pytest.mark.parametrize("path", _backtests(), ids=lambda p: p.name)
def test_every_backtest_is_a_manipulations_harness(path: Path) -> None:
    """The other half: the backtests are manipulations harnesses. If one stops importing the
    manipulations path, "the backtest covers манипуляции" needs re-checking too."""
    src = path.read_text(encoding="utf-8")
    assert any(m in src for m in _MANIP_MARKERS), (
        f"{path.name} references none of {_MANIP_MARKERS} — is it still a manipulations "
        "harness? CLAUDE.md claims every backtest_*.py is one."
    )


def test_prizrak_does_not_import_the_scanner() -> None:
    """Независимость модулей, направление 1. Shared plumbing (market/, data/, features/) is
    fine and expected; reaching into the OTHER strategy's detection is not."""
    offenders = [
        p.relative_to(_ROOT).as_posix()
        for p in _PRIZRAK.rglob("*.py")
        if _imports_module(p, "hunt_core.scanner")
    ]
    assert not offenders, f"prizrak reaches into the scanner: {offenders}"


def test_scanner_does_not_import_prizrak() -> None:
    """Независимость модулей, направление 2."""
    offenders = [
        p.relative_to(_ROOT).as_posix()
        for p in _SCANNER.rglob("*.py")
        if _imports_module(p, "hunt_core.prizrak")
    ]
    assert not offenders, f"scanner reaches into prizrak: {offenders}"
