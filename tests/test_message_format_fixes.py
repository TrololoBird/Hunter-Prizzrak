"""Verified message-format defects from the 2026-07-16 output audit.

Each test fails on the pre-fix code:

- FIX 1 query_service.format_query_telegram: the graceful «анализ временно
  недоступен» fallback fell through to ``analysis.row`` → NameError → the user
  got NO message on exactly the path the fallback existed for.
- FIX 2 build.interest_zones_text: hardcoded «WAIT-тика — не активный сигнал»
  caption rendered inside every DELIVERED long/short card.
- FIX 3 analyst_assembly.send_analyst_change_telegram: unguarded
  ``rr_primary`` → literal «R:R (от входа) None» in the activation header.
- FIX 4 the pinned push carried no as-of stamp (broadcaster replays buffered
  cards after circuit-open).
- FIX 5 signal_queue.format_queue_telegram: uppercased lookup against
  lowercase map keys → raw English enum leaked.
- FIX 6 _followup TP2 close: printed the TP2 price under a «PnL» label.
- FIX 7 RU pluralization: «4 касаний».
- FIX 8 build: unescaped invalidation conditions / driver names.
"""
from __future__ import annotations

import asyncio
from typing import Any

from hunt_core.prizrak.build import AnalystReport


# ── FIX 1 — the fallback card must actually be returned ───────────────────────


def test_report_build_failure_returns_fallback_card(monkeypatch: Any) -> None:
    from hunt_core.runtime import query_service as qs

    def _boom(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("synthetic build failure")

    monkeypatch.setattr("hunt_core.prizrak.build.build_deep_report", _boom)

    q = qs.build_query_result(
        symbol="BTCUSDT",
        row={"symbol": "BTCUSDT", "price": 60000.0},
        source="analyst_assembly",
        from_store=False,
        age_s=None,
    )
    # Pre-fix: NameError('analysis') — the user got nothing at all.
    text = qs.format_query_telegram(q)
    assert "анализ временно недоступен" in text
    assert "BTCUSDT" in text


# ── FIX 2 — no WAIT claim inside an active signal ─────────────────────────────


def _zones_report(action: str) -> AnalystReport:
    row = {
        "prizrak_interest_zones": {
            "tf": "4h",
            "long": {"lo": 60173.0, "hi": 60507.0, "touches": 4},
            "long_ladder": [{"lo": 60173.0, "hi": 60507.0, "touches": 4}],
        },
        "prizrak_structure": {"htf_bias": {"bias": "long"}},
        "prizrak_summary": {"action": action},
    }
    return AnalystReport(symbol="BTCUSDT", row=row, fusion={}, forecasts={}, would_deliver=False)


def test_active_signal_zone_block_makes_no_wait_claim() -> None:
    txt = _zones_report("long").interest_zones_text()
    assert "WAIT" not in txt
    assert "не активный сигнал" not in txt
    assert "отдельно от основного сетапа" in txt


def test_wait_tick_zone_block_keeps_wait_caption() -> None:
    txt = _zones_report("wait").interest_zones_text()
    assert "не активный сигнал" in txt


# ── FIX 3 / FIX 4 — activation header R:R + pinned as-of stamp ────────────────


class _StubResult:
    status = "sent"
    message_id = 1
    reason = None


class _StubBroadcaster:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_html(self, text: str) -> _StubResult:
        self.sent.append(text)
        return _StubResult()


def _pinned_row(rr: Any) -> dict[str, Any]:
    return {
        "symbol": "BTCUSDT",
        "price": 60000.0,
        "as_of": "2026-07-16T10:00:00",
        "ts": "2026-07-16T10:00:00+00:00",
        "prizrak_summary": {"action": "long", "rr_primary": rr, "setup_kind": "level_core"},
    }


def _send_pinned(row: dict[str, Any]) -> str:
    from hunt_core.runtime.analyst_assembly import send_analyst_change_telegram

    bc = _StubBroadcaster()
    ok = asyncio.run(send_analyst_change_telegram(bc, row, lifecycle_event="activated"))
    assert ok, "stub broadcaster should have accepted the card"
    return bc.sent[0]


def test_activation_header_omits_rr_when_none(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "hunt_core.prizrak.arbiter.evaluate_deep_delivery", lambda **_kw: (True, [])
    )
    text = _send_pinned(_pinned_row(None))
    assert "None" not in text.split("\n")[0]
    assert "R:R (от входа)" not in text


def test_activation_header_formats_rr_two_decimals(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "hunt_core.prizrak.arbiter.evaluate_deep_delivery", lambda **_kw: (True, [])
    )
    text = _send_pinned(_pinned_row(2.5))
    assert "R:R (от входа) <code>2.50</code>" in text


def test_pinned_push_carries_as_of_stamp(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "hunt_core.prizrak.arbiter.evaluate_deep_delivery", lambda **_kw: (True, [])
    )
    text = _send_pinned(_pinned_row(2.5))
    assert "2026-07-16 10:00:00 UTC" in text
    assert text.count("2026-07-16 10:00:00 UTC") == 1


# ── FIX 5 — lifecycle RU lookup is reachable ─────────────────────────────────


def test_queue_lifecycle_ru_lookup_hits_lowercase_phases() -> None:
    from hunt_core.prizrak.engines.signal_queue import format_queue_telegram

    txt = format_queue_telegram(
        {
            "top3": [
                {
                    "symbol": "BTCUSDT",
                    "action": "long",
                    "lifecycle": "pre_pump",
                    "opportunity_score": 0.8,
                    "rank": 1,
                }
            ]
        }
    )
    assert "накопление" in txt
    assert "pre_pump" not in txt
    assert "PRE_PUMP" not in txt


# ── FIX 6 — TP2 close reports a real PnL, not the TP2 price ───────────────────


class _Followup:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.symbol = "BTCUSDT"
        self.direction = "long"
        self.price = 62_000.0
        self.payload = payload
        self.event = "fix_profit_tp2"
        self.detail = ""


def test_tp2_close_shows_computed_pnl_percent() -> None:
    from hunt_core.deliver._followup import format_followup_telegram

    fu = _Followup(
        {
            "entry_lo": 60_000.0,
            "entry_hi": 60_000.0,
            "tp2": 63_000.0,
            "opened_at": "2026-07-16T08:00:00",
        }
    )
    txt = format_followup_telegram(fu, {})
    assert "+5.00%" in txt  # 60000 → 63000 long
    assert "PnL: TP2" not in txt  # the price is no longer labelled as PnL


# ── FIX 7 — RU pluralization ─────────────────────────────────────────────────


def test_touches_ru_pluralization() -> None:
    from hunt_core.prizrak.build import _touches_ru

    assert _touches_ru(1) == "1 касание"
    assert _touches_ru(2) == "2 касания"
    assert _touches_ru(4) == "4 касания"
    assert _touches_ru(5) == "5 касаний"
    assert _touches_ru(11) == "11 касаний"
    assert _touches_ru(14) == "14 касаний"
    assert _touches_ru(21) == "21 касание"
    assert _touches_ru(22) == "22 касания"


def test_zone_strength_uses_correct_plural() -> None:
    txt = _zones_report("wait").interest_zones_text()
    assert "4 касания" in txt
    assert "4 касаний" not in txt


# ── FIX 8 — HTML escaping at the render site ─────────────────────────────────


def test_invalidation_and_drivers_are_escaped() -> None:
    row = {
        "prizrak_summary": {
            "action": "long",
            "entry_lo": 60_000.0,
            "entry_hi": 60_100.0,
            "stop_loss": 59_000.0,
            "invalidation": [{"condition": "close > 61000 & vol < 1M"}],
            "confluence_drivers": [{"name": "OI > 5%", "delta": 0.1}],
        },
    }
    report = AnalystReport(
        symbol="BTCUSDT", row=row, fusion={}, forecasts={}, would_deliver=False
    )
    txt = report.prizrak_text()
    assert "close &gt; 61000 &amp; vol &lt; 1M" in txt
    assert "OI &gt; 5%" in txt
