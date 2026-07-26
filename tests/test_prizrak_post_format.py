"""Prizrak-post formatter — pins the author's post grammar the deep card must render.

The card is now :func:`~hunt_core.prizrak.format_post.format_prizrak_post` (razbor
prizrak_bch_praktikum §7): zones in the author's own layout (🟢 перезакуп (ПОК) / 🟡 добор /
🔴 шорт + 💰 цели, per horizon), a «🌪 По приборам» narrative sourced only from real producers,
«🤔 По совокупности» from confluence, and nothing fabricated when a source is absent (I-6).
"""
from __future__ import annotations

import pytest

from _deep_fixtures import make_report
from hunt_core.features.models import FeaturePanel, TfSummary
from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.prizrak.setups import build_symbol_setups

_CFG = PrizrakConfig.load()


@pytest.fixture(autouse=True)
def _no_tracker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hermetic: the optional ✅-closed line must never read production tracker state in a test."""
    monkeypatch.setattr("hunt_core.track.tracker.load_tracker_state", lambda *a, **k: {})


def _bar(o: float, h: float, low: float, c: float, v: float = 100.0) -> list[float]:
    return [0.0, o, h, low, c, v]


def _flat_base(*, lo: float, hi: float, cycles: int, vol: float = 100.0) -> list[list[float]]:
    bars: list[list[float]] = []
    mid = (lo + hi) / 2
    for _ in range(cycles):
        bars.append(_bar(mid, hi * 1.001, mid * 0.999, hi * 0.999, vol))
        bars.append(_bar(hi * 0.999, hi, mid, mid, vol))
        bars.append(_bar(mid, mid * 1.001, lo * 0.999, lo * 1.001, vol))
        bars.append(_bar(lo * 1.001, mid, lo, mid, vol))
    return bars


def _post(**over: object) -> str:
    from hunt_core.prizrak.format_post import format_prizrak_post

    return format_prizrak_post(make_report(**over))  # type: ignore[arg-type]


def _features(rsi4: float, rsi1: float, *, diver: bool | None = False) -> FeaturePanel:
    """Per-TF panel with RSI + divergence flags set (``None`` diver ⇒ flags unknown)."""
    return FeaturePanel(
        symbol="BCHUSDT",
        now_ms=0,
        tf={
            "4h": TfSummary(rsi14=rsi4, bearish_rsi_div=diver, bullish_rsi_div=diver),
            "1h": TfSummary(rsi14=rsi1, bearish_rsi_div=diver, bullish_rsi_div=diver),
        },
    )


def test_post_renders_author_grammar_with_poc_perezakup() -> None:
    """🟢 перезакуп anchors on the ПОК, targets show, приборы states RSI + «диверов нет»."""
    bars = _flat_base(lo=100.0, hi=110.0, cycles=10, vol=500.0)
    bars.append(_bar(110.0, 130.0, 110.0, 128.0))
    bars += [_bar(128.0, 129.0, 127.0, 128.0) for _ in range(4)]
    setups = build_symbol_setups({"4h": bars}, price=128.0, cfg=_CFG)

    out = _post(
        symbol="BCHUSDT",
        price=128.0,
        setups=setups,
        structure={"htf_bias": {"bias": "long", "regime": ""}},
        features=_features(48.0, 52.0),
    )
    assert "#BCH" in out
    assert "Зоны интереса" in out
    assert "перезакуп" in out and "ПОК" in out
    # приборы: sourced only from real producers, and «нет» only over computed-False flags (I-6).
    assert "RSI" in out and "4ч 48" in out and "1ч 52" in out
    assert "диверов 1ч/4ч нет" in out
    assert "не инвестрекомендация" in out


def test_empty_setups_never_fabricates_zones() -> None:
    """I-6: no horizon zones → an explicit «нет зон» note, never a 🟢/🔴 rung out of nowhere."""
    out = _post(
        symbol="BCHUSDT",
        price=128.0,
        setups={"horizons": {}, "price": 128.0, "bias": ""},
        features=_features(50.0, 50.0),
    )
    assert "Качественных зон" in out
    assert "🟢" not in out and "🔴 шорт" not in out


def test_diver_tokens_omitted_when_unknown() -> None:
    """A warm-up (divergence flags ``None``) must NOT print «диверов нет» — the token is dropped."""
    out = _post(
        symbol="BCHUSDT",
        price=128.0,
        setups={"horizons": {}, "price": 128.0, "bias": ""},
        features=_features(50.0, 50.0, diver=None),
    )
    assert "диверов" not in out
    assert "RSI" in out  # RSI still shown — it was computed


def test_counter_trend_short_is_absent_from_the_card() -> None:
    """Контр-трендовый/отработанный шорт в карточку не попадает вовсе.

    Помечать зону «по факту» и одновременно предлагать её в плане — значит печатать «бери» и
    «не бери» в одном сообщении; замерено на живом BTC 2026-07-26, где метка стояла на КАЖДОЙ
    зоне и потому не значила ничего."""
    bars = _flat_base(lo=100.0, hi=110.0, cycles=10)
    bars.append(_bar(105.0, 105.5, 104.5, 105.0))  # sit inside → straddle → ceiling short
    setups = build_symbol_setups(
        {"4h": bars}, price=105.0, cfg=_CFG, structure={"htf_bias": {"bias": "long"}}
    )
    out = _post(
        symbol="BCHUSDT",
        price=105.0,
        setups=setups,
        structure={"htf_bias": {"bias": "long", "regime": ""}},
        features=_features(55.0, 55.0),
    )
    assert "🔴 шорт" not in out
    # И НЕ молчание: карточка обязана сказать, что зоны были и почему их сняли (I-6).
    assert "Чистых зон нет" in out


def test_sovokupnost_from_confluence_drivers() -> None:
    """«🤔 По совокупности» names the positive confluence drivers off the summary."""
    out = _post(
        symbol="BCHUSDT",
        price=128.0,
        setups={"horizons": {}, "price": 128.0, "bias": ""},
        summary={
            "action": "wait",
            "confluence_drivers": [
                {"name": "HTF-тренд", "delta": 0.12},
                {"name": "объём базы", "delta": 0.06},
                {"name": "против ликвидаций", "delta": -0.04},
            ],
        },
        features=_features(50.0, 50.0),
    )
    assert "По совокупности" in out
    assert "комбо:" in out and "HTF-тренд" in out


def test_pochemu_net_sdelki_from_abstain_on_wait() -> None:
    """A WAIT tick folds «почему нет сделки» (RR reason) from PrizrakOutput.abstain into the post."""
    out = _post(
        symbol="BCHUSDT",
        price=218.0,
        setups={"horizons": {}, "price": 218.0, "bias": ""},
        summary={"action": "wait"},
        abstain=[{"reason": "rr_below_floor", "rr": 2.3, "min_rr": 3.0}],
        features=_features(50.0, 50.0),
    )
    # `<` is HTML-escaped to `&lt;` (the message is sent as HTML) — assert the rendered form.
    assert "почему нет сделки" in out and "RR 2.3 &lt; 3.0" in out



def test_active_signal_shows_entry_stop_tp() -> None:
    """An emitted LONG/SHORT renders its tracked trade plan (вход/стоп/цели/R:R), not just the label."""
    out = _post(
        symbol="BTCUSDT", price=65000.0, setups={"horizons": {}, "price": 65000.0, "bias": ""},
        summary={
            "action": "long", "entry_lo": 62000.0, "entry_hi": 62500.0, "stop": 60800.0,
            "stop_anchor": "structure", "rr_primary": 2.4, "tp_ladder": [64000.0, 66000.0, 68000.0],
        },
        features=_features(50.0, 50.0),
    )
    assert "вход" in out and "62000" in out and "62500" in out
    assert "стоп" in out and "60800" in out and "за структуру" in out
    assert "R:R" in out and "2.4" in out
    assert "цели" in out and "64000" in out


def test_no_active_plan_on_wait() -> None:
    """A WAIT tick invents no entry/stop/TP plan (I-6)."""
    out = _post(
        symbol="BTCUSDT", price=65000.0, setups={"horizons": {}, "price": 65000.0, "bias": ""},
        summary={"action": "wait"}, features=_features(50.0, 50.0),
    )
    assert "вход <code>" not in out and "стоп <code>" not in out




def test_no_pochemu_net_sdelki_when_signal_active() -> None:
    """An active LONG/SHORT must NOT print «почему нет сделки» (there IS a trade)."""
    out = _post(
        symbol="BCHUSDT",
        price=218.0,
        setups={"horizons": {}, "price": 218.0, "bias": ""},
        summary={"action": "long"},
        abstain=[{"reason": "rr_below_floor", "rr": 2.3, "min_rr": 3.0}],
        features=_features(50.0, 50.0),
    )
    assert "почему нет сделки" not in out
