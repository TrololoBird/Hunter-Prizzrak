"""POC / VAH / VAL on a found накопление zone — the centerpiece confirmed twice this
session: independent recomputation matched PrizrakTrade's own visually-marked POC
almost exactly on both ONDO (0.310-0.311 vs his 0.3114) and BTC (60,271 vs his
60,511.9/60,173.3/59,978.7 bracket).

Reuses ``features.volume_profile.volume_profile_levels`` verbatim (the project's own
fixed-range histogram implementation) — no reimplementation of the bucket math.
"""
from __future__ import annotations

from typing import Any

import polars as pl

from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.features.volume_profile import volume_profile_levels


def _frame_from_ohlcv(ohlcv: list[list[float]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "high": [float(r[2]) for r in ohlcv],
            "low": [float(r[3]) for r in ohlcv],
            "volume": [float(r[5]) for r in ohlcv],
        }
    )


_MIN_STRUCTURE_BARS = 5


def _structure_bars(
    ohlcv: list[list[float]], zone: dict[str, Any] | None
) -> list[list[float]]:
    """Bars spanned by the structure, or the full window if it can't be located.

    Two producers, two key names, one meaning — "the bars this structure occupies":
    an accumulation zone marks its span with ``first_touch_idx``/``last_touch_idx``
    (boundary-touch pivots, accumulation._zone_from_clusters), a стоповый объём with
    ``structure_lo_idx``/``structure_hi_idx`` (a dense sub-window; it has no touches
    to count — stop_volume.find_stop_volume).
    """
    if not zone:
        return ohlcv
    first = zone.get("structure_lo_idx", zone.get("first_touch_idx"))
    last = zone.get("structure_hi_idx", zone.get("last_touch_idx"))
    if first is None or last is None:
        return ohlcv
    lo_i, hi_i = int(first), int(last) + 1
    if lo_i < 0 or hi_i > len(ohlcv) or hi_i - lo_i < _MIN_STRUCTURE_BARS:
        return ohlcv
    return ohlcv[lo_i:hi_i]


def zone_poc(
    ohlcv: list[list[float]],
    *,
    zone: dict[str, Any] | None = None,
    cfg: PrizrakConfig | None = None,
) -> dict[str, Any]:
    """POC/VAH/VAL over the given bars, optionally restricted to a found накопление zone.

    ``ohlcv`` is the tier lookback the zone (from ``accumulation.find_accumulation_zone``)
    was found on; the zone's ``first_touch_idx``/``last_touch_idx`` index into it. The
    profile is fitted to those bars alone, per course methodology (с. 26: "натягивая
    профиль на структуру — важно захватить все свечи структуры"). Profiling the whole
    lookback instead put the POC outside the zone on a large share of candidates, which
    is not a POC of that накопление at all.
    """
    cfg = cfg or PrizrakConfig.load()
    bars = _structure_bars(ohlcv, zone)
    frame = _frame_from_ohlcv(bars)
    poc, vah, val = volume_profile_levels(frame, buckets=cfg.vp_buckets, value_area_pct=cfg.vp_value_area_pct)
    if poc is None:
        return {}
    out: dict[str, Any] = {"poc": round(poc, 8), "vah": round(vah, 8) if vah else None, "val": round(val, 8) if val else None}
    if zone:
        lo, hi = zone.get("lo"), zone.get("hi")
        if lo and hi and hi > lo:
            out["poc_stable"] = _poc_is_stable(frame, poc, lo=float(lo), hi=float(hi), cfg=cfg)
            position = (poc - lo) / (hi - lo)  # 0=at support, 1=at resistance
            # A profile fitted to the structure can still peak just outside the boundary
            # band (the zone's hi/lo are cluster means, not hard extremes), so keep the
            # guard rather than emit a ratio that isn't one.
            if 0.0 <= position <= 1.0:
                out["poc_position_in_zone"] = round(position, 4)
    return out


# Доля ширины зоны, на которую ПОК может гулять при смене разбиения корзин, оставаясь «устойчивым».
# Порог измерен: у одномодальных структур разброс 1.8–5.0% ширины (ETH 1653–1851, SOL 66.9–74.3,
# AVAX 6.14–6.87), у бимодальной LTC 40.78–45.59 — 54.1%. Между этими режимами провал, 15% лежит в нём.
_POC_STABILITY_MAX_SPREAD = 15.0
# Разбиения для пробы. Одномодальный пик остаётся тем же при любом из них; на двух почти равных
# пиках argmax перескакивает — это и есть наблюдаемый симптом бимодальности.
_POC_STABILITY_BUCKETS = (40, 60, 90, 120)


def _poc_is_stable(frame: pl.DataFrame, poc: float, *, lo: float, hi: float,
                   cfg: PrizrakConfig) -> bool:
    """Устойчив ли ПОК к смене разбиения — то есть один ли у профиля доминирующий пик.

    Зачем. Вход якорится на ПОК (стр.30), но на БИМОДАЛЬНОЙ зоне это ложная точность: измерено на
    LTC 4h 40.78–45.59 профилем по 4785 пятнадцатиминуткам — два пика несут 12.9% и 12.4% объёма,
    и argmax перескакивает между ними, сдвигая якорь входа на 5.7%. Курс это предвидит прямо:
    «до POC может не дойти» (стр.30), поэтому он и разносит закуп на 2–3 ордера, а не ставит один.

    Мерить сами моды напрямую я пробовал и получил самоопровергающийся детектор (окно ±6 корзин
    при шаге 6 покрыло 52 корзины из 60 → «все зоны четырёхмодальны, по 25% каждая»). Здесь
    меряется НАБЛЮДАЕМЫЙ симптом — перескок ПОК при смене разбиения, — а не гипотеза о форме.
    """
    span = hi - lo
    if span <= 0:
        return True
    seen: list[float] = [poc]
    for buckets in _POC_STABILITY_BUCKETS:
        if buckets == cfg.vp_buckets:
            continue
        p, _vah, _val = volume_profile_levels(
            frame, buckets=buckets, value_area_pct=cfg.vp_value_area_pct
        )
        if p is not None:
            seen.append(float(p))
    if len(seen) < 3:
        return True  # мерить нечем — не объявляем неустойчивость без основания (I-6)
    return (max(seen) - min(seen)) / span * 100.0 <= _POC_STABILITY_MAX_SPREAD


__all__ = ["zone_poc"]
