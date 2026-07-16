"""Advisory (unconfirmed) manipulation cards must not re-send every scan cycle."""
from __future__ import annotations

import hunt_core.deliver.manipulation_delivery as md


def test_advisory_throttled_within_cooldown(monkeypatch) -> None:
    md._ADVISORY_SENT.clear()
    key = ("EPICUSDT", "long", "C")
    md._ADVISORY_SENT[key] = (1000.0, 1)  # sent at t=1000, steps=1
    # within cooldown, no progress → throttled
    assert 1 <= md._ADVISORY_SENT[key][1]
    now = 1000.0 + md._ADVISORY_RESEND_S - 1
    prev = md._ADVISORY_SENT[key]
    throttled = prev is not None and 1 <= prev[1] and (now - prev[0]) < md._ADVISORY_RESEND_S
    assert throttled is True


def test_advisory_resends_on_progress_or_expiry() -> None:
    md._ADVISORY_SENT.clear()
    key = ("EPICUSDT", "long", "C")
    md._ADVISORY_SENT[key] = (1000.0, 1)
    prev = md._ADVISORY_SENT[key]
    # progress: steps grew 1 → 2 (e.g. LTF confirm) → must re-send
    assert not (2 <= prev[1])
    # expiry: cooldown lapsed → must re-send
    now = 1000.0 + md._ADVISORY_RESEND_S + 1
    assert not ((now - prev[0]) < md._ADVISORY_RESEND_S)
