"""Стоповый объём — a small, dense sub-range that holds price (near/inside a larger
накопление) while bigger players add to position. No existing primitive for this
(confirmed during exploration) — implemented directly here.

Detection: slide a short window over the bars used to find the parent zone; a
candidate is a sub-window whose price width is well below the parent zone's width
(``stop_volume_width_ratio_max``) AND whose average volume is at or above the parent
window's average (density, not just narrowness — a quiet narrow patch isn't a stop
volume, a busy narrow patch is).
"""
from __future__ import annotations

from typing import Any

from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.prizrak.poc import _MIN_STRUCTURE_BARS

# Ширина подокна, в котором ищется стоповый объём. `find_stop_volume` штампует span победителя
# как structure_lo_idx/structure_hi_idx (ширина = _SUB_WINDOW), а `poc._structure_bars` его
# профилирует — но только если в span'е >= _MIN_STRUCTURE_BARS баров, иначе он МОЛЧА
# откатывается на всё окно и возвращает ПОК РОДИТЕЛЯ (ровно тот дефект, ради которого стр.35
# даёт стоповому СВОЙ ПОК).
#
# ⚠ АССЕРТ СВЯЗЫВАЕТ ДВЕ КОНСТАНТЫ, А НЕ СТАВИТ ПОТОЛОК ОДНОЙ. Прежняя формулировка провоцировала
# чтение «6 — потолок _MIN_STRUCTURE_BARS»; это неверно. Замер 2026-07-27: T=7/8/10/12 в одиночку
# роняют ИМПОРТ (AssertionError → orchestrator → весь ПРИЗРАК), но T=7 + _SUB_WINDOW=7 и
# T=20 + _SUB_WINDOW=20 импортируются штатно. Настоящая цена подъёма — не «падает призрак», а
# «меняется геометрия стопового объёма», и она на живых данных НЕ измерена.
#
# ⚠ Ссылка на пиннинг-тест снята: каталог tests/ удалён 2026-07-27, и с тех пор этот ассерт —
# ЕДИНСТВЕННЫЙ механический читатель связки (I-8: ссылка на несуществующий якорь гниёт молча).
_SUB_WINDOW = 6
assert _SUB_WINDOW >= _MIN_STRUCTURE_BARS, (
    f"_SUB_WINDOW ({_SUB_WINDOW}) < poc._MIN_STRUCTURE_BARS ({_MIN_STRUCTURE_BARS}): span "
    "стопового объёма короче минимума профиля — zone_poc молча вернёт ПОК РОДИТЕЛЬСКОГО окна"
)


def find_stop_volume(
    ohlcv: list[list[float]],
    *,
    zone: dict[str, Any],
    cfg: PrizrakConfig | None = None,
) -> dict[str, Any]:
    """Best (highest-density, narrowest) sub-window candidate within ``ohlcv``, or {}."""
    cfg = cfg or PrizrakConfig.load()
    if len(ohlcv) < _SUB_WINDOW + 2 or not zone:
        return {}

    zone_width_pct = zone.get("width_pct")
    if not zone_width_pct or zone_width_pct <= 0:
        return {}

    parent_avg_vol = sum(r[5] for r in ohlcv) / len(ohlcv)
    if parent_avg_vol <= 0:
        return {}

    best: dict[str, Any] = {}
    best_score = -1.0
    for i in range(len(ohlcv) - _SUB_WINDOW + 1):
        window = ohlcv[i:i + _SUB_WINDOW]
        hi = max(r[2] for r in window)
        lo = min(r[3] for r in window)
        if lo <= 0:
            continue
        width_pct = (hi - lo) / lo * 100
        ratio = width_pct / zone_width_pct
        if ratio > cfg.stop_volume_width_ratio_max:
            continue
        avg_vol = sum(r[5] for r in window) / len(window)
        density = avg_vol / parent_avg_vol
        if density < 1.0:
            continue  # narrow but not busy — not a stop volume, just quiet chop
        score = density / max(ratio, 0.01)  # reward density, penalize width
        if score > best_score:
            best_score = score
            best = {
                "hi": round(hi, 8), "lo": round(lo, 8),
                "width_pct": round(width_pct, 4), "width_ratio_to_zone": round(ratio, 3),
                "volume_density": round(density, 2),
                "bar_start_ts": window[0][0], "bar_end_ts": window[-1][0],
                # Bar span of the стоповый's own sub-window, so poc.zone_poc can pull a
                # fixed-range profile over exactly these bars: стр.35 gives the стоповый
                # its OWN ПОК, and стр.26 requires the profile cover the structure's own
                # candles — without a span zone_poc profiles the whole ТФ-1 lookback and
                # returns a level that is not this стоповый's ПОК at all.
                #
                # Deliberately NOT named first_touch_idx/last_touch_idx like an
                # accumulation zone's span (accumulation._zone_from_clusters). Those names
                # promise boundary-touch pivots, which a стоповый has none of (it is found
                # by density+narrowness), and two OTHER readers of that pair would then
                # duck-type an sv as a zone: _stop_volume_bars indexes it into the NATIVE-TF
                # ohlcv while these indices are ТФ-1-relative, and accumulation._zone_volume
                # assumes touch semantics. Distinct names keep both refusals honest.
                #
                # Must stay >= poc._MIN_STRUCTURE_BARS, or _structure_bars silently falls
                # back to the whole window and hands back the PARENT's ПОК — the exact bug
                # this span exists to prevent. Механически это держит ассерт при импорте
                # (см. _SUB_WINDOW выше); прежняя ссылка на пиннинг-тест снята вместе с
                # каталогом tests/ 2026-07-27 — она указывала в пустоту (I-8).
                "structure_lo_idx": i, "structure_hi_idx": i + _SUB_WINDOW - 1,
            }
    return best


__all__ = ["find_stop_volume"]
