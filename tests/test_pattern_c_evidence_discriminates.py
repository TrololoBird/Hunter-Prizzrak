"""Pattern C «почему» must discriminate, not restate preconditions (WO #4).

asc/desc/zakrep are hard gates, so those tokens are on every emitted C. The evidence
must lead with VARYING factors (reclaim depth, micro state, HTF bias) so two C setups
with different context read differently.
"""
from __future__ import annotations

from hunt_core.deliver.manipulation_delivery import _split_evidence
from hunt_core.scanner.detect.patterns import ManipulationSetup


def _setup(evidence: tuple[str, ...], direction: str = "long") -> ManipulationSetup:
    return ManipulationSetup(direction=direction, pattern_type="C", score=0.7, evidence=evidence)


def test_varying_factors_change_the_reason_line() -> None:
    a = _split_evidence(_setup(("закреп×2", "ltf_pending", "prior_swing_high", "htf_bull")))
    b = _split_evidence(_setup(("закреп×5", "ltf_confirmed", "prior_swing_high")))
    assert a != b  # different context → different «почему»/risks


def test_ltf_pending_is_a_risk_not_support() -> None:
    supporting, risks = _split_evidence(_setup(("закреп×2", "ltf_pending", "prior_swing_high")))
    assert any("ltf" in r.lower() or "подтвержд" in r.lower() or r for r in risks)
    assert "ltf_pending" not in supporting


def test_canonical_htf_bear_flags_counter_bias_risk() -> None:
    # htf_bear on a LONG must be a RISK (canonical token now emitted, not htf_bullish).
    _sup, risks = _split_evidence(_setup(("htf_bear", "закреп×3"), direction="long"))
    assert risks  # counter-bias surfaced as a risk
