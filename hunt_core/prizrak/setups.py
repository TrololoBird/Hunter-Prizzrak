"""Мульти-горизонт PRIZRAK-сетапы — «4 сетапа на актив» в стиле реальных постов автора
(локальный / недельный / снайпер / спот), КАЖДАЯ зона ПОК-якорена.

Слой ДЕРИВАЦИИ для формата постов (``format_post.py``). Заменяет одно-ТФ отображение
``compute_interest_zones`` там, где нужна карта зон в грамматике автора. Переиспользует ту же
машинерию накоплений/ПОК — ничего в движке эмиссии не меняет:

* ``find_accumulation_zones`` (accumulation.py) — боксы 4+ касаний;
* ``zone_poc`` (poc.py) — ПОК/VAH/VAL, натянутый на бары структуры (стр.26);
* ``_split_below_above`` (orchestrator) — декомпозиция straddle-бокса на грани;
* ``_level_already_worked`` / ``detect_level_saw`` — курс стр.25/31/28.

Что нового против ``compute_interest_zones``:

1. **ПОК на КАЖДОЙ зоне** — фикс замеренного бага (BCH: лонг-вход был ВЕРХ бокса 218 вместо ПОК
   внизу 196). Лонг-якорь = ПОК (стр.30 «надёжнее всего брать от уровня ПОК»), не край у цены.
2. **Мульти-горизонт** — локальный (4h) и недельный (1d/1w) считаются ОБА, не fallback-один-ТФ.
   Спот-горизонт добавляет формат-слой из ``native.spot_ladder`` (полная 10-лет история, ATL).
3. **Грамматика зон**: 🟢 перезакуп (сильнейшая ПОК-зона) · 🟡 добор (ближние опорные) · 🔴 шорт.
4. **«По факту»** — контр-трендовые/отработанные зоны помечаются ``by_fact=True`` (не дропаются, как
   в авто-сигнале — автор именно так и торгует «по факту слома»).
"""
from __future__ import annotations

from typing import Any

from hunt_core.prizrak.accumulation import find_accumulation_zones
from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.prizrak.orchestrator import (
    _INTEREST_ZONE_MAX_WIDTH_PCT,
    _level_already_worked,
    _split_below_above,
    _tf_lookback_map,
)
from hunt_core.prizrak.poc import zone_poc
from hunt_core.prizrak.structure import bars_from_ohlcv
from hunt_core.prizrak.traps import detect_level_saw

# Horizon → ordered TF preference (first with usable zones wins per horizon). Local = the near
# 4h/1h map the trader trades intraday-to-swing; weekly = the 1d/1w levels his «шорт от недельного»
# / «глобальный» setups anchor to. Spot is merged separately from the full-history spot ladder.
# Разбор ASTR (2026-07-25) показал, что автор работает на ТРЁХ горизонтах сразу и называет их
# явно: «уровень поддержки 4ч ТФ — 0.005059», «ближайший уровень сопротивления 0.005170» и
# «лонг от уровня поддержки 1ч ТФ». Ключевое — на кадре 27 он ПЕРЕКЛЮЧАЕТСЯ на 15-минутный
# график и размечает 0.005177/0.005165 именно там. Без внутридневного горизонта этот уровень
# физически не выражался: на 4ч его нет, на 1ч он размазан в зону 0.005093–0.005282 шириной
# 3.71%, а на 15м это 0.005150–0.005186 — 0.70% и 56 касаний, почти точно его кромки.
_HORIZONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("intraday", ("15m", "5m")),
    ("local", ("4h", "1h")),
    ("weekly", ("1d", "1w")),
)
_LADDER_MAX = 3

# Level-map lookback per TF. The tier CANDIDATE lookback (meso=60 → floor 120 ≈ 20 days on 4h) is far
# too short for a LEVEL MAP: it clips deep-but-recent support out of the window, so nothing below price
# surfaces. The case that produced these numbers (BCH snapshot, 2026-07-22 — a dated observation, not a
# standing fact: price has since returned to that band): the author's headline 🟢190–205 ПОК196 base had
# last traded ~127×4h bars back, JUST outside a 120-bar window, so a win=120 map showed only shorts above
# price while win≈250 surfaced the 196 база cleanly. The general rule is what matters — a level map must
# outlive the swing that left the level, and the author explicitly works from a fuller chart («проверь,
# полный ли график»). These windows cover the current swing structure's whole accumulation without
# reaching into a prior regime; ``raw[-lookback:]`` is a no-op when fewer bars exist.
_SETUP_LOOKBACK: dict[str, int] = {"5m": 500, "15m": 400, "1h": 360, "4h": 300, "1d": 365, "1w": 260}


def _course_flags(bars: list[dict[str, float]], *, level: float, side: str) -> dict[str, Any]:
    """Курс стр.25/31/28 verdict at ``level``: reacted-off (worked) / «пила» (saw) / limit_ok."""
    worked = _level_already_worked(bars, level=level, direction=side)
    saw = detect_level_saw(bars, level=level)
    return {"worked": int(worked), "saw": bool(saw), "limit_ok": worked < 1 and not saw}


def _num(x: Any) -> float | None:
    """Numeric-or-None narrowing for the VRVP outputs (poc/val/vah can be None with no profile)."""
    return float(x) if isinstance(x, (int, float)) else None


def _fact_reason(
    flags: dict[str, Any], *, side: str, bias: str, is_perezakup: bool
) -> tuple[bool, str]:
    """«По факту» verdict + reason for a zone. Two regimes:

    * **Перезакуп** (author's PRIMARY ПОК re-buy): a prior reaction is EXPECTED — you re-buy a level
      that held — and does NOT disqualify the limit. Here the video refines PDF стр.31 (practicum:
      он лимитно перезакупает ПОК даже после реакции). So flag it «по факту» ONLY when it is counter
      to the HTF bias — «шорты держу, по ключевым зонам добираю лонги … по факту» (BTC/ETH обзор).
    * **Добор / шорт rung** (a fresh set-and-forget limit): стр.31/28 hold — a worked level is «только
      по слому», a sawn one is waited out; counter-trend is «по факту слома». All → flagged, not dropped.
    """
    counter = (side == "long" and bias == "short") or (side == "short" and bias == "long")
    if is_perezakup:
        return (counter, "против тренда" if counter else "")
    if flags.get("saw"):
        return (True, "пила")
    if int(flags.get("worked") or 0) >= 1:
        return (True, "отработан")
    return (counter, "против тренда" if counter else "")


def _zone_view(
    raw_window: list[list[float]],
    bars: list[dict[str, float]],
    z: dict[str, Any],
    *,
    side: str,
    cfg: PrizrakConfig,
    bias: str,
) -> dict[str, Any]:
    """A добор/шорт rung: **value area** of the box + its ПОК + course flags + by_fact.

    Порядок у автора — ЗОНА → индикатор НА ней → УРОВЕНЬ: структурный бокс это ВХОД профиля, а
    торгуемый уровень — его ВЫХОД (курс стр.24 и кейс ZEN стр.31 разделяют их прямо: «зона и POC —
    два раздельных объекта отработки»). Перезакуп это уже делал (:func:`_perezakup_view` сужает
    базу до [VAL, VAH]), а добор и шорт публиковали СЫРЫЕ кромки бокса, выбрасывая vah/val, —
    то есть вход индикатора вместо выхода.

    Сужение применяется ТОЛЬКО когда value area реально пересекает бокс: декомпозированная
    straddle-полоса несёт индексы родительской структуры, поэтому её профиль считается по чужим
    барам и может лежать целиком вне полосы — тогда остаётся бокс, а не подставная область (I-6).
    ПОК показывается лишь внутри итоговой полосы. Вход — ПОК, иначе кромка (long=hi, short=lo)."""
    lo, hi = float(z["lo"]), float(z["hi"])
    info = zone_poc(raw_window, zone=z, cfg=cfg)
    poc, val, vah = _num(info.get("poc")), _num(info.get("val")), _num(info.get("vah"))
    if val is not None and vah is not None and val < vah and not (vah < lo or val > hi):
        n_lo, n_hi = max(lo, val), min(hi, vah)
        if n_lo < n_hi:
            lo, hi = n_lo, n_hi
    poc_in = poc is not None and lo <= poc <= hi
    edge = poc if (poc_in and poc is not None) else (hi if side == "long" else lo)
    flags = _course_flags(bars, level=edge, side=side)
    by_fact, reason = _fact_reason(flags, side=side, bias=bias, is_perezakup=False)
    return {
        "lo": round(lo, 8), "hi": round(hi, 8),
        "poc": (round(poc, 8) if (poc_in and poc is not None) else None),
        "touches": int(z.get("touches") or 0), "entry": round(edge, 8),
        "by_fact": by_fact, "fact_reason": reason, **flags,
    }


def _perezakup_view(
    raw_window: list[list[float]],
    bars: list[dict[str, float]],
    base: dict[str, Any],
    *,
    cfg: PrizrakConfig,
    bias: str,
    price: float,
) -> dict[str, Any] | None:
    """🟢 Перезакуп = the **value area [VAL, VAH] of the dominant volume base**, anchored at ПОК.

    This is the author's «перезакуп лонга … здесь ПОК крупной структуры» (стр.30): re-buy the
    volume core of the whole base, NOT a tight near-price box. Measured fix — BCH gave the box top
    218; the base's value area is 190–205 with ПОК 196, exactly the author's zone. Falls back to the
    box range when VRVP has no profile (crypto-spot etc.).

    A re-buy band lives BELOW price, so both edges are clipped there: the base's BOX sits below
    price, but the structure bars it spans may wick above, which drags VAL/VAH/ПОК up with them.
    Returns ``None`` when the whole value area sits above price — that base has no re-buy band left,
    and inventing one by re-ordering the edges would advertise a "buy zone" you can only enter by
    buying INTO resistance (measured on ARPA: VAL 0.008686 > price 0.008320 rendered as a
    «перезакуп 0.008320–0.008686», i.e. a long entry above spot). ПОК is likewise reported only when
    it lands inside the final band — never a foreign ПОК (I-6), same guard as :func:`_zone_view`."""
    lo_box, hi_box = float(base["lo"]), float(base["hi"])
    info = zone_poc(raw_window, zone=base, cfg=cfg)
    poc, val, vah = _num(info.get("poc")), _num(info.get("val")), _num(info.get("vah"))
    hi = min(vah if vah is not None else hi_box, price)
    lo = min(val if val is not None else lo_box, hi)
    if not lo < hi:
        return None
    anchor = poc if (poc is not None and lo <= poc <= hi) else hi
    flags = _course_flags(bars, level=anchor, side="long")
    by_fact, reason = _fact_reason(flags, side="long", bias=bias, is_perezakup=True)
    return {
        "lo": round(lo, 8), "hi": round(hi, 8),
        "poc": (round(poc, 8) if (poc is not None and lo <= poc <= hi) else None),
        "touches": int(base.get("touches") or 0), "entry": round(anchor, 8),
        "by_fact": by_fact, "fact_reason": reason, **flags,
    }


def _tight(side: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Limit-band width gate (стр.31 «вход по факту касания» must be a tight band, not a 12% box)."""
    narrow = [z for z in side if float(z.get("width_pct") or 0) <= _INTEREST_ZONE_MAX_WIDTH_PCT]
    return narrow or (sorted(side, key=lambda z: float(z.get("width_pct") or 0))[:1] if side else [])


def _horizon_zones(
    raw: list[list[float]], *, price: float, cfg: PrizrakConfig, use_tf: str, bias: str
) -> dict[str, Any] | None:
    """🟢 перезакуп (value area of the volume base) · 🟡 добор (tight support above it) · 🔴 шорт."""
    lookback = max(_SETUP_LOOKBACK.get(use_tf, 200), _tf_lookback_map(cfg).get(use_tf, 120))
    window = raw[-lookback:]
    bars = bars_from_ohlcv(window)
    if not bars:
        return None
    zones = find_accumulation_zones(bars, tf=use_tf, cfg=cfg, max_zones=8)
    if not zones:
        return None

    out: dict[str, Any] = {"tf": use_tf}
    # 🟢 ПЕРЕЗАКУП — value area of the strongest-VOLUME base whose box sits BELOW price (a genuine
    # re-buy support, not the current range). NOT tight-filtered: that base carries the ПОК the
    # author re-buys (стр.30). ⚠️ Precision residual: find_accumulation_zones can MERGE a deeper
    # sub-base into one box, so the ПОК lands on recent volume, not the sub-base (BCH: our ~217 vs
    # the author's June-sub-base 196). Reaching the sub-base ПОК needs «база-в-базе» detection (A4).
    below_boxes = [z for z in zones if float(z["hi"]) < price]
    base = max(below_boxes, key=lambda z: float(z.get("zone_volume") or 0)) if below_boxes else None
    vah: float | None = None
    if base is not None:
        # None ⇒ that base's value area sits entirely above price: no re-buy band, and `vah` stays
        # None so the добор/цели filters below fall back to "no value-area floor" instead of
        # inheriting a bogus one.
        pk = _perezakup_view(window, bars, base, cfg=cfg, bias=bias, price=price)
        if pk is not None:
            out["perezakup"] = pk
            vah = pk["hi"]
    # 🟡 ДОБОР — tight support boxes ABOVE the value area, below price (nearer add-to-long rungs);
    # straddle floors included via the long-side decomposition.
    below_long, _ = _split_below_above(zones, price=price, decompose_short=False)
    dobor_src = [
        z for z in _tight(below_long)
        if float(z["hi"]) < price and (vah is None or float(z["hi"]) > vah) and z is not base
    ]
    dobor_src.sort(key=lambda z: z["hi"], reverse=True)  # nearest-first
    if dobor_src:
        out["dobor"] = [
            _zone_view(window, bars, z, side="long", cfg=cfg, bias=bias) for z in dobor_src[:_LADDER_MAX]
        ]
    # 🔴 ШОРТ — near resistance (tight above-boxes + straddle ceilings), nearest first.
    _, above = _split_below_above(zones, price=price, decompose_short=True)
    short_src = sorted(_tight(above), key=lambda z: z["lo"])[:_LADDER_MAX]
    if short_src:
        out["short"] = [
            _zone_view(window, bars, z, side="short", cfg=cfg, bias=bias) for z in short_src
        ]
    if "perezakup" not in out and "dobor" not in out and "short" not in out:
        return None
    # Цели: opposing structural levels (long → resistance above the value area; short → below).
    if vah is not None:
        ups = sorted(float(z["lo"]) for z in zones if float(z.get("lo") or 0) > vah)
        if ups:
            out["long_targets"] = ups[:3]
    if short_src:
        anchor_hi = float(short_src[0]["lo"])
        downs = sorted((float(z["hi"]) for z in zones if 0 < float(z.get("hi") or 0) < anchor_hi), reverse=True)
        if downs:
            out["short_targets"] = downs[:3]
    return out


def _headroom(horizons: dict[str, Any], *, price: float) -> dict[str, Any] | None:
    """Ход до БЛИЖАЙШЕГО встречного уровня по всем горизонтам, вверх и вниз.

    Разбор ASTR (2026-07-25) вскрыл, что «есть уровень» и «есть сделка» — разные вопросы. Уровень
    0.005059 у автора отличный (204 касания на 700 барах, самый нагруженный в серии), и он всё
    равно отказался: «процент движения между уровнями слишком небольшой». Арифметика его отказа:
    от цены до встречного сопротивления 2.33%, а от его зоны закупа до того же уровня 7.27% —
    в 3.1 раза больше при том же типе стопа (за структуру 1–3%, PDF стр. 33).

    Карточка при этом показывала R:R 8.2, потому что мерила до ДАЛЬНЕЙ цели и не видела стену в
    137 касаний на +1.31%. Здесь считается честное расстояние до первого препятствия; решение
    ((«тесно») принимает форматтер, а гейтом эмиссии это сознательно НЕ становится — 2.33%/7.27%
    пока одно наблюдение, и порог по одной точке — ровно тот класс ошибки, от которого защищает
    ``docs/HUNTER_TARGET_SPEC.md`` §1.

    Меряется КОРИДОР, а не одна сторона: он формулирует это как «от этого уровня до этого уровня»,
    то есть его интересует ширина между ближайшей поддержкой и ближайшим сопротивлением, а не
    расстояние до одного из них. Односторонняя цифра к тому же вырождается: когда цена стоит у
    кромки зоны, «до встречного уровня 0.04%» технически верно и совершенно бесполезно.

    Returns:
        ``{"up_price", "down_price", "width_pct"}`` — ``width_pct`` только когда найдены ОБЕ
        стороны (иначе коридора нет и ширину считать не из чего); ``None``, если встречных
        уровней нет вообще (не 0.0 — I-6).
    """
    if price <= 0:
        return None
    ups: list[float] = []
    downs: list[float] = []
    for hz in horizons.values():
        if not isinstance(hz, dict):
            continue
        for key in ("perezakup", "dobor", "short"):
            raw = hz.get(key)
            zones = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
            for z in zones:
                if not isinstance(z, dict):
                    continue
                for edge in ("lo", "hi"):
                    try:
                        lv = float(z[edge])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if lv > price:
                        ups.append(lv)
                    elif lv < price:
                        downs.append(lv)
    out: dict[str, Any] = {}
    if ups:
        out["up_price"] = min(ups)
    if downs:
        out["down_price"] = max(downs)
    if "up_price" in out and "down_price" in out and out["down_price"] > 0:
        out["width_pct"] = round((out["up_price"] / out["down_price"] - 1.0) * 100.0, 2)
    return out or None


def build_symbol_setups(
    ohlcv_by_tf: dict[str, list[list[float]]],
    *,
    price: float,
    cfg: PrizrakConfig,
    structure: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Мульти-горизонт карта зон (intraday + local + weekly), ПОК-якорена. Спот мёржится в форматтере.

    Returns ``{"horizons": {name: {tf, perezakup?, dobor?, short?, long_targets?, short_targets?}},
    "price": price, "headroom": {...}}`` — empty ``horizons`` when no usable zone on any TF. Never
    fabricates: a horizon is absent when its TFs have no qualifying accumulation box (I-6).
    """
    if price <= 0:
        return {"horizons": {}, "price": price}
    htf = (structure or {}).get("htf_bias") if isinstance(structure, dict) else None
    bias = str(htf.get("bias") or "").lower() if isinstance(htf, dict) else ""
    horizons: dict[str, Any] = {}
    for name, tfs in _HORIZONS:
        for use_tf in tfs:
            raw = ohlcv_by_tf.get(use_tf)
            if not raw:
                continue
            hz = _horizon_zones(raw, price=price, cfg=cfg, use_tf=use_tf, bias=bias)
            if hz is not None:
                horizons[name] = hz
                break  # first TF with usable zones wins for this horizon
    out: dict[str, Any] = {"horizons": horizons, "price": float(price), "bias": bias}
    headroom = _headroom(horizons, price=float(price))
    if headroom is not None:
        out["headroom"] = headroom
    return out


__all__ = ["build_symbol_setups"]
