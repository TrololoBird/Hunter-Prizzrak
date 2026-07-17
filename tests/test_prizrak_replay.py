"""Пиннинг форвард-реплея Призрака (research/prizrak_replay.py).

Реплей — это ИЗМЕРИТЕЛЬ, и без собственного теста он не инструмент: молчаливый lookahead
или фантомный филл сделали бы его цифры такими же ложными, как леджер, ради замены которого
он и написан. Здесь пиннятся два его инварианта — вход только по факту касания зоны, и исход
touch-based (стоп/цель по форвардному пути), — плюс граница модулей (импортов Манипуляций нет).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

# research/ is not a package (matches tests/test_no_lookahead_sweep.py's loader).
_path = Path(__file__).resolve().parent.parent / "research" / "prizrak_replay.py"
_spec = importlib.util.spec_from_file_location("_prizrak_replay_under_test", _path)
assert _spec and _spec.loader
replay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(replay)


def _bar(ts: int, o: float, h: float, low: float, c: float) -> list[float]:
    return [ts, o, h, low, c, 100.0]


def test_unfilled_limit_is_no_trade_not_a_loss() -> None:
    """Курс стр.30: вход ЛИМИТКОЙ на тесте. Если цена не вернулась в entry-зону в окне
    ожидания — сделки нет (None), а НЕ убыток. Фантомный леджер путал это: план = факт."""
    sig = {"action": "long", "entry_lo": 100.0, "entry_hi": 101.0, "stop": 98.0, "tp1": 106.0}
    # цена уходит вверх и не возвращается к зоне 100-101
    seg = [_bar(i, 110 + i, 111 + i, 109 + i, 110 + i) for i in range(30)]
    assert replay._resolve(seg, sig) is None


def test_fill_then_target_is_a_win_in_R() -> None:
    sig = {"action": "long", "entry_lo": 100.0, "entry_hi": 101.0, "stop": 98.0, "tp1": 106.0}
    # бар 0 касается зоны → fill на entry_mid=100.5, risk=|100.5-98|=2.5; TP1=106 → 2.2R
    seg = [_bar(0, 100.8, 101.0, 100.0, 100.8)] + [_bar(i, 103, 107, 102, 106) for i in range(1, 5)]
    out = replay._resolve(seg, sig)
    assert out is not None and out[0] == "win"
    assert out[1] == (106.0 - 100.5) / 2.5  # (tp1 - entry_mid) / risk(entry_mid→stop)


def test_fill_then_stop_is_a_loss_minus_one_R() -> None:
    sig = {"action": "long", "entry_lo": 100.0, "entry_hi": 101.0, "stop": 98.0, "tp1": 106.0}
    seg = [_bar(0, 100.8, 101.0, 100.0, 100.8)] + [_bar(i, 99, 99.5, 97.0, 98.0) for i in range(1, 5)]
    out = replay._resolve(seg, sig)
    assert out == ("loss", -1.0)


def test_stop_checked_before_target_on_a_bar_spanning_both() -> None:
    """Консервативный read: бар, накрывший и стоп, и цель, засчитывается СТОПОМ."""
    sig = {"action": "long", "entry_lo": 100.0, "entry_hi": 101.0, "stop": 98.0, "tp1": 106.0}
    seg = [_bar(0, 100.5, 101.0, 100.0, 100.5), _bar(1, 100, 107, 97, 103)]  # 2-й бар: и 97, и 107
    assert replay._resolve(seg, sig) == ("loss", -1.0)


def test_short_side_mirrors() -> None:
    sig = {"action": "short", "entry_lo": 99.0, "entry_hi": 100.0, "stop": 102.0, "tp1": 94.0}
    seg = [_bar(0, 99.5, 100.0, 99.0, 99.5)] + [_bar(i, 96, 97, 93, 94) for i in range(1, 5)]
    out = replay._resolve(seg, sig)
    assert out is not None and out[0] == "win"


def test_closed_upto_never_returns_a_forming_bar() -> None:
    """I-5: бар входит в срез только когда ПОЛНОСТЬЮ закрыт (ts + dur <= T)."""
    rows = [_bar(i * replay.TF_MS["1h"], 1, 1, 1, 1) for i in range(10)]
    t = 5 * replay.TF_MS["1h"]  # ровно на открытии 6-го бара
    got = replay._closed_upto(rows, "1h", t)
    # последний закрытый = бар с ts=4h (закрылся в 5h == T); форминг-бар ts=5h НЕ входит
    assert got[-1][0] == 4 * replay.TF_MS["1h"]
    assert all(r[0] + replay.TF_MS["1h"] <= t for r in got)
