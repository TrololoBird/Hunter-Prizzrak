"""Coiling/preparation readiness scoring for prescan (P0-B).

Two separate outputs — never one number:
- ``energy`` 0..100: coiling / preparation strength (NOT price change).
- ``direction``: bull | bear | undecided — **сегодня из ОДНОГО pos-in-range**.

``abs(change_pct)`` is metadata only — forbidden as primary ranking key.

⚠ Что этот файл НЕ делает, вопреки прежнему описанию. К 2026-07-26 отсюда сняты три полосы
подряд — фандинг, BB-squeeze и поток (delta/CVD) — и все три по одной причине: функция бежит по
ВСЕЙ вселенной поверх ``fetch_ticker_24h``, а ``market/symbols.py::normalize_ticker_rows`` отдаёт
ровно 8 полей (symbol / last_price / price_change_percent / quote_volume / trade_count /
underlying_type / high_price / low_price). Ключей, на которых гейтились полосы, там нет вовсе,
поэтому они не начисляли ничего ни разу. Вместе с потоком ушёл и ``fake_energy_veto``.
Осталось честно: energy = объём + OI + частота сделок, direction = положение в диапазоне.
**Настоящая продуктовая дыра — «ловец пружин» на слое ВОРОНКИ** (отбор тихих поджатий до первого
движения). Она теперь видна, а не спрятана за кодом, который выглядел работающим.

Moved here from the now-deleted ``hunt_core/expansion/`` package: this was the
one piece of that package genuinely wired into a live scoring path
(``scanner/prescan.py``, ~55% weight in the prescan score) — everything else in
that package (the opportunity/forecast/execution/alerts/learning stack, the
``/expand`` Telegram command, two background tasks) was gated behind
``ExpansionConfig.is_lab_runtime`` (default off, never turned on: no runtime-state
file, no calibration file, zero graded outcomes ever) and duplicated the Scanner
module's own pre-pump/pre-dump mission. Deleted rather than left dormant.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal

from hunt_core.data.baseline_store import SymbolBaseline, baseline_zscores, load_baseline

Direction = Literal["bull", "bear", "undecided"]

_ENERGY_MIN = 15.0


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v):
        return default
    return v


def _pos_in_range(last: float, high: float | None, low: float | None) -> float | None:
    if high is None or low is None or high <= low or last <= 0:
        return None
    return max(0.0, min(1.0, (last - low) / (high - low)))


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _z_component(z: float | None, *, weight: float = 1.0) -> float:
    # Energy rewards SURGES (see the vol/OI/trade comment below), so only a
    # positive z-score — activity ABOVE the rolling mean — contributes. The old
    # abs(z) let a drought (z ≪ 0: volume/trade far BELOW average) inflate energy
    # exactly like a surge, scoring dead coins as "about to expand" (SCAN-2).
    # oi_accel already gates oi_z > 0 before calling, so it is unaffected.
    if z is None:
        return 0.0
    return _clamp01(max(z, 0.0) / 4.0) * weight * 100.0


@dataclass(frozen=True, slots=True)
class ExpansionReadiness:
    symbol: str
    energy: float
    direction: Direction
    bull_score: float
    bear_score: float
    change_24h_pct: float
    reasons: tuple[str, ...]


def compute_expansion_readiness(
    row: dict[str, Any],
    *,
    baseline: SymbolBaseline | None = None,
    oi_change_pct: float | None = None,
) -> ExpansionReadiness | None:
    sym = str(row.get("symbol") or "").strip().upper()
    last = _safe_float(row.get("last_price"))
    if not sym or last is None or last <= 0:
        return None

    base = baseline or load_baseline(sym)
    zs = baseline_zscores(base)
    change = _safe_float(row.get("price_change_percent") or row.get("price_change_pct"), 0.0) or 0.0
    high = _safe_float(row.get("high_price") or row.get("high_24h"))
    low = _safe_float(row.get("low_price") or row.get("low_24h"))
    pos = _pos_in_range(last, high, low)
    oi_chg = oi_change_pct if oi_change_pct is not None else _safe_float(row.get("oi_change_pct"))
    # УДАЛЕНА полоса ПОТОКА (`delta_ratio`/`agg_trade_delta_30s`/`cvd_slope`/`session_cvd_slope`) —
    # третья и последняя по счёту в этом файле, по тому же прецеденту, что фандинг и BB-squeeze.
    # Ни один из четырёх ключей не пишет НИ ОДИН продюсер строк тикера, поэтому:
    #   • `flow_known` был вечно False → `fake_energy_veto` не взводился НИКОГДА, и
    #     `readiness_meets_prescan` вырождался в `energy >= min_energy`;
    #   • полосы ±10 (OI+дельта) и ±12 (CVD) не начисляли ничего → `direction` и так решал
    #     один pos-in-range.
    # Удаление — тождество и на `energy`, и на `direction` (проверено ниже по каждой ветке).
    #
    # Заполнить на ЭТОМ слое нечем, и это структурно, а не «руки не дошли»: функция бежит по
    # ВСЕЙ вселенной (~500 символов) поверх `fetch_ticker_24h`, а bulk-эндпоинта taker-потока
    # у Binance нет; движок знает поток только по ПРОГРЕТЫМ символам. Настоящий поток живёт на
    # аналитическом пути (`view.orderflow`, `engine/orderflow.py::delta_ratio`) — там он и
    # считается. Возвращать полосу сюда можно ТОЛЬКО вместе с продюсером на строке тикера.
    # (2026-07-26, по аудиту осиротевших ключей)

    # is-None-fallthrough, а НЕ `or`: z-скор ровно 0.0 — законное измерение («последняя
    # выборка равна медиане окна»), robust_z отдаёт None, когда мерить нечего. `or` выбрасывал
    # измеренное «всплеска на 5m нет» и молча подставлял 24-часовой z, который может быть большим,
    # раздувая energy — а energy решает допуск символа в юниверс сканирования. Тот же приём стоял
    # на delta/cvd (полоса снята выше); здесь его не применили. (2026-07-26)
    vol_z = zs.get("volume_z_5m")
    if vol_z is None:
        vol_z = zs.get("volume_z")
    # `oi_z`/`oi_z_5m` больше не публикуются `baseline_store.baseline_zscores` — у серии
    # `baseline.oi` нет продюсера, и она была ЗАМОРОЖЕНА (замер: 1000BONKUSDT — 288 точек,
    # 10 уникальных значений, последние 12 идентичны), из-за чего `z_1h` отдавал +2.08 из
    # истории, а `_z_component(oi_z, weight=0.5)` превращал это в 50 из 100 очков energy.
    # Чтение оставлено: ключи вернутся сами, как только появится настоящий продюсер.
    oi_z = zs.get("oi_z_5m")
    if oi_z is None:
        oi_z = zs.get("oi_z")
    trade_z = zs.get("trade_rate_z")
    oi_accel = 0.0
    if oi_chg is not None and oi_chg > 0:
        oi_accel = _clamp01(oi_chg / 5.0) * 20.0
    elif oi_z is not None and oi_z > 0:
        oi_accel = _z_component(oi_z, weight=0.5)

    # NOTE (audit): a BB-squeeze lane (15 pts) and an accumulation / "ловец пружин"
    # lane (30 pts) sat here and were deleted as unreachable — together 45 of the 100
    # energy points were structurally unable to score. Both keyed off
    # `row["bb_width_pct"] or row["bb_width"]`, and this function only ever runs on
    # `fetch_ticker_24h` rows, which carry no such key — so `squeeze` was always None
    # and both lanes were pinned at 0.0. Deleting them is an identity on `energy`.
    #
    # They were dead a second time over, which is why reviving them here would not have
    # worked either: the scoring math (`1.0 - min(squeeze, 1.0)`) reads squeeze as a
    # RATIO, while the only bb_width this project computes (features/prepare_frame.py)
    # is a PERCENT — so any real producer feeding this would still have scored 0 for
    # every band wider than 1%.
    #
    # The methodology's BB-squeeze factor is NOT lost: it lives on the analyst path in
    # prizrak/confluence.py (`_bb_width_pctile`, a unit-safe percentile) where klines
    # are actually in hand. What IS a real product gap is a spring-catcher at the FUNNEL
    # layer — selecting quiet coils before the first move. That gap is now visible
    # instead of hidden behind code that looked like it worked.
    energy_parts = [
        _z_component(vol_z, weight=0.35),
        oi_accel,
        _z_component(trade_z, weight=0.20),
    ]
    energy = round(min(100.0, sum(energy_parts)), 1)

    # УДАЛЁН `fake_energy_veto` — «OI и объём выстрелили, но потока за ними НЕТ» (ложный пробой).
    # Вердикт осмыслен, только когда поток ИЗМЕРЕН, а измерять его здесь нечем (см. выше), поэтому
    # `flow_known` был вечно False и вето не взводилось ни разу. Формула оставалась в файле,
    # выглядела рабочей и держала поле `fake_energy_veto` в контракте — ровно тот случай, когда
    # мёртвый код читается как гарантия. Гейт `readiness_meets_prescan` теперь честно говорит,
    # что фильтрует ОДНУ величину. Воскрешать вместе с продюсером потока.

    bull = 0.0
    bear = 0.0
    if pos is not None:
        if pos <= 0.35:
            bull += 25.0
        elif pos >= 0.75:
            bear += 25.0
    # УДАЛЕНА полоса фандинга (±15 очков), по прецеденту двух предыдущих полос выше.
    # `row["funding_rate"]` не пишет НИ ОДИН продюсер строк: единственный источник —
    # `market/symbols.py::normalize_ticker_rows` — отдаёт symbol/last_price/
    # price_change_percent/quote_volume/trade_count/underlying_type/high_price/low_price.
    # Значит `funding` всегда None, и полоса не начисляла ничего никогда. Удаление —
    # тождество на `direction`, но теперь дыра ВИДНА, а не спрятана за кодом, который
    # выглядит работающим. Настоящий фандинг живёт на аналитическом пути
    # (`view.derivs.funding`, `scanner/feed.py::_funding_for`) — воскрешать здесь можно
    # только вместе с продюсером на строке тикера. (2026-07-26)
    # Здесь стояли ещё две полосы направления — ±10 (OI>1% И знак дельты) и ±12 (знак CVD).
    # Обе гейтились на потоке, которого на строке тикера нет, поэтому не начисляли ничего
    # никогда. Снято вместе с полосой потока: `direction` фактически решает ОДИН pos-in-range,
    # и теперь это видно из кода, а не только из замера.
    if bull > bear + 8.0:
        direction: Direction = "bull"
    elif bear > bull + 8.0:
        direction = "bear"
    else:
        direction = "undecided"

    reasons: list[str] = []
    if vol_z is not None:
        reasons.append(f"vol_z={vol_z:.1f}")
    if oi_z is not None:
        reasons.append(f"oi_z={oi_z:.1f}")
    reasons.append(f"dir={direction}")

    return ExpansionReadiness(
        symbol=sym,
        energy=energy,
        direction=direction,
        bull_score=round(bull, 1),
        bear_score=round(bear, 1),
        change_24h_pct=round(change, 2),
        reasons=tuple(reasons[:5]),
    )


def readiness_meets_prescan(readiness: ExpansionReadiness, *, min_energy: float = _ENERGY_MIN) -> bool:
    """Пропуск в воронку сканирования: ОДИН порог по энергии, второго условия больше нет.

    Раньше здесь стояло `and not readiness.fake_energy_veto`. Вето требовало ИЗМЕРЕННОГО потока,
    которого на строке тикера не бывает, поэтому терм был константой True с самого переписывания
    транспорта. Гейт не изменился по поведению — изменилась его честность.
    """
    return readiness.energy >= min_energy


__all__ = [
    "Direction",
    "ExpansionReadiness",
    "compute_expansion_readiness",
    "readiness_meets_prescan",
]
