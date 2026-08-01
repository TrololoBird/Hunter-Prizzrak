"""Замер: устойчивы ли VAL/VAH к смене разбиения — и надо ли их гардить, как ПОК.

Зачем. `setups.py::_perezakup_view` обрезает публикуемую полосу по [VAL, VAH] (строки 192-193)
БЕЗУСЛОВНО, а флаг `poc_stable` считается ПОСЛЕ (строка 196) и охраняет только якорь. То есть
когда профиль объявлен двугорбым, предупреждение вешается на ПОК, а границы зоны, выведенные
из ТОГО ЖЕ профиля, публикуются без оговорки. Вопрос: заслуживают ли они оговорки.

Гипотеза, которую замер обязан проверить, а не подтвердить. ПОК — это **argmax** одной
корзины: при двух почти равных модах он ПЕРЕСКАКИВАЕТ, и это разрыв. VAL/VAH — границы
области, накрывающей заданную долю объёма, то есть **интеграл**: смена разбиения смещает их
плавно. Если так, VAL/VAH окажутся заметно устойчивее ПОКа и отдельный гард им не нужен —
и тогда честный результат замера это «дефекта нет», а не «нашёл ещё один».

Метод повторяет `poc.py::_poc_is_stable` буквально, чтобы числа были сравнимы:
профиль натягивается на бары структуры, разброс нормируется на ШИРИНУ ЗОНЫ, перебираются те
же разбиения (40/60/90/120) и те же сдвиги начала сетки (0.25, 0.5).

Меряется ТРИ величины, и третья — главная:
  * разброс VAL и VAH по отдельности;
  * разброс ПОКа на тех же зонах — база для сравнения;
  * **сдвиг ИТОГОВОЙ полосы** [max(lo,VAL), min(hi,VAH)] — именно она публикуется и торгуется.
    Полоса может почти не шевелиться даже при гуляющих VAL/VAH: обрезка берёт максимум и
    минимум с кромками бокса, и когда value area шире бокса, она не влияет вовсе.

Запуск:
    uv run python scripts/verify_value_area_stability.py
    uv run python scripts/verify_value_area_stability.py --write
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import pathlib

import ccxt.pro as ccxtpro
import polars as pl

from hunt_core.features.volume_profile import volume_profile_levels
from hunt_core.prizrak.accumulation import find_accumulation_zones
from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.prizrak.poc import _structure_bars
from hunt_core.prizrak.setups import bars_from_ohlcv

REPORT = pathlib.Path("docs/audit/value-area-stability-2026-07-27.md")
_SYMBOLS = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT",
    "XRP/USDT:USDT", "DOGE/USDT:USDT", "LINK/USDT:USDT",
]
_TFS = ("15m", "1h", "4h")
_BUCKETS = (40, 60, 90, 120)
_ORIGINS = (0.0, 0.25, 0.5)


def _frame(bars: list[list[float]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "high": [float(b[2]) for b in bars],
            "low": [float(b[3]) for b in bars],
            "volume": [float(b[5]) for b in bars],
        }
    )


def _spread_pct(values: list[float], span: float) -> float | None:
    """Разброс величины в процентах от ширины зоны — та же нормировка, что у ПОКа."""
    clean = [v for v in values if v is not None]
    if len(clean) < 3 or span <= 0:
        return None
    return (max(clean) - min(clean)) / span * 100.0


def measure_zone(
    bars: list[list[float]], zone: dict, cfg: PrizrakConfig, *, price: float
) -> dict | None:
    """Разброс POC / VAL / VAH и ОБЕИХ публикуемых полос под смену разбиения.

    ⚠ ПЕРВАЯ РЕДАКЦИЯ ЭТОГО ЗАМЕРА МЕРИЛА НЕ ТУ ПОЛОСУ. Она считала обрезку `_zone_view`
    (`max(lo, VAL)`, `min(hi, VAH)`) — пересечение бокса и value area, где кромки бокса
    ограничивают value area с обеих сторон. Оттуда и вышел успокоительный результат «медиана
    сдвига полосы 0.0%».

    Но у ПЕРЕЗАКУПА обрезка другая (`_perezakup_view:192-193`):
        hi = min(vah or hi_box, price)
        lo = min(val or lo_box, hi)
    — бокс здесь только запасной вариант НА СЛУЧАЙ None, а не ограничитель. Значит
    неустойчивость VAL/VAH проходит в публикуемую полосу один к одному. Именно эта полоса и
    есть предмет вопроса, и она измерена не была.

    Меряется в ДВУХ единицах. Проценты ширины зоны — для сравнения с гардом ПОКа. Ширины
    корзин при N=40 — потому что вендорная неопределённость VAH/VAL составляет ±1 строку
    профиля (TradingView останавливается ДО превышения цели, Sierra — ПОСЛЕ), и сдвиг в одну
    корзину это не нестабильность, а разница конвенций. В процентах ширины зоны один и тот же
    порог означал бы разную строгость при разном N.
    """
    lo_box, hi_box = float(zone["lo"]), float(zone["hi"])
    span = hi_box - lo_box
    if span <= 0:
        return None
    struct = _structure_bars(bars, zone)
    frame = _frame(struct)
    if frame.height < 5:
        return None
    pocs: list[float] = []
    vals: list[float] = []
    vahs: list[float] = []
    zv_lo: list[float] = []
    zv_hi: list[float] = []
    pk_lo: list[float] = []
    pk_hi: list[float] = []
    for buckets in _BUCKETS:
        for origin in _ORIGINS:
            poc, vah, val = volume_profile_levels(
                frame, buckets=buckets,
                value_area_pct=cfg.vp_value_area_pct, origin_shift=origin,
            )
            if poc is None:
                continue
            pocs.append(float(poc))
            if val is None or vah is None or val >= vah:
                continue
            vals.append(float(val))
            vahs.append(float(vah))
            # добор/шорт — пересечение бокса и value area
            zv_lo.append(max(lo_box, float(val)))
            zv_hi.append(min(hi_box, float(vah)))
            # перезакуп — value area с боксом лишь как None-fallback
            p_hi = min(float(vah), price)
            p_lo = min(float(val), p_hi)
            if p_lo < p_hi:
                pk_lo.append(p_lo)
                pk_hi.append(p_hi)
    # Пол квантования: одна корзина самого грубого разбиения, в процентах ширины зоны.
    prof_range = max(float(b[2]) for b in struct) - min(float(b[3]) for b in struct)
    bucket_w = prof_range / min(_BUCKETS) if prof_range > 0 else 0.0
    floor_pct = bucket_w / span * 100.0 if span > 0 else 0.0
    ranges = sorted(float(b[2]) - float(b[3]) for b in struct)
    bar_range_med = ranges[len(ranges) // 2] if ranges else 0.0
    return {
        "poc": _spread_pct(pocs, span),
        "val": _spread_pct(vals, span),
        "vah": _spread_pct(vahs, span),
        "zv_lo": _spread_pct(zv_lo, span),
        "zv_hi": _spread_pct(zv_hi, span),
        "pk_lo": _spread_pct(pk_lo, span),
        "pk_hi": _spread_pct(pk_hi, span),
        "floor_pct": floor_pct,
        # во сколько раз медианный бар шире корзины при cfg.vp_buckets — потолок разрешения
        "bar_over_bucket": (
            bar_range_med / (prof_range / cfg.vp_buckets)
            if prof_range > 0 and cfg.vp_buckets else None
        ),
        "n_profiles": len(pocs),
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    cfg = PrizrakConfig()
    ex = ccxtpro.binanceusdm({"options": {"defaultType": "swap"}, "enableRateLimit": True})
    await ex.load_markets()
    rows: list[dict] = []
    skipped: list[str] = []
    try:
        for sym in _SYMBOLS:
            for tf in _TFS:
                try:
                    bars = await ex.fetch_ohlcv(sym, tf, limit=500)
                except Exception as exc:  # noqa: BLE001 — недоступный ТФ не приговор замеру
                    # Но знаменатель в итоге («N символов × M ТФ») печатается ПЛАНОВЫЙ,
                    # а не фактический: без учёта пропусков он завышает охват замера.
                    skipped.append(f"{sym}/{tf}: {exc.__class__.__name__}")
                    continue
                if len(bars) < 60:
                    continue
                shaped = bars_from_ohlcv(bars)
                if not shaped:
                    continue
                price = float(bars[-1][4])
                zones = find_accumulation_zones(shaped, tf=tf, cfg=cfg, max_zones=8)
                for z in zones:
                    if not isinstance(z, dict) or not z.get("lo") or not z.get("hi"):
                        continue
                    m = measure_zone(bars, z, cfg, price=price)
                    if m and m["poc"] is not None and m["val"] is not None:
                        rows.append({"symbol": sym, "tf": tf, **m})
    finally:
        await ex.close()

    out: list[str] = []

    def say(line: str = "") -> None:
        print(line)
        out.append(line)

    if skipped:
        say(f"**ПОКРЫТИЕ НЕПОЛНОЕ** — не загружено пар символ/ТФ: {len(skipped)} "
            f"из {len(_SYMBOLS) * len(_TFS)}")
        for s in skipped[:20]:
            say(f"  - {s}")
        if len(skipped) > 20:
            say(f"  - … и ещё {len(skipped) - 20}")
        say()

    if not rows:
        say("зон не найдено — замерять нечего")
        return

    loaded_pairs = len(_SYMBOLS) * len(_TFS) - len(skipped)
    say(f"настоящих зон промерено: **{len(rows)}** "
        f"(пар символ/ТФ загружено {loaded_pairs} из {len(_SYMBOLS) * len(_TFS)}, "
        f"{len(_BUCKETS)}×{len(_ORIGINS)} профилей на зону)")
    say()
    say("Разброс в процентах от ШИРИНЫ ЗОНЫ (нормировка та же, что у `_poc_is_stable`):")
    say()
    say("| величина | медиана | p90 | максимум | зон с разбросом >15% |")
    say("|---|---|---|---|---|")
    for key, title in (
        ("poc", "ПОК (argmax)"), ("val", "VAL"), ("vah", "VAH"),
        ("zv_lo", "добор/шорт — низ полосы"), ("zv_hi", "добор/шорт — верх полосы"),
        ("pk_lo", "ПЕРЕЗАКУП — низ полосы"), ("pk_hi", "ПЕРЕЗАКУП — верх полосы"),
    ):
        vs = sorted(r[key] for r in rows if r.get(key) is not None)
        if not vs:
            continue
        n = len(vs)
        over = sum(1 for v in vs if v > 15.0)
        say(f"| {title} | {vs[n // 2]:.1f}% | {vs[min(n - 1, int(n * 0.9))]:.1f}% | "
            f"{vs[-1]:.1f}% | {over} из {n} ({over / n * 100:.0f}%) |")
    say()

    # Прямое сравнение на КАЖДОЙ зоне: устойчивее ли value area, чем ПОК.
    pairs = [(r["poc"], max(r["val"], r["vah"])) for r in rows
             if r.get("poc") is not None and r.get("val") is not None]
    va_calmer = sum(1 for p, v in pairs if v < p)
    say(f"На **{va_calmer} из {len(pairs)}** зон ({va_calmer / len(pairs) * 100:.0f}%) "
        "value area шатается МЕНЬШЕ, чем ПОК.")
    say()

    # Пол квантования и потолок разрешения — без них любой порог необоснован.
    floors = sorted(r["floor_pct"] for r in rows if r.get("floor_pct"))
    if floors:
        n = len(floors)
        say(f"**Пол квантования** (одна корзина при N={min(_BUCKETS)}, в % ширины зоны): "
            f"медиана {floors[n // 2]:.1f}%, p90 {floors[min(n - 1, int(n * 0.9))]:.1f}%. "
            "Разброс НИЖЕ этого — чистая дискретизация сетки, а не поведение рынка.")
        say()
    ratios = sorted(r["bar_over_bucket"] for r in rows if r.get("bar_over_bucket"))
    if ratios:
        n = len(ratios)
        over = sum(1 for v in ratios if v > 1.0)
        say(f"**Потолок разрешения**: медианный бар шире корзины в {ratios[n // 2]:.1f}× "
            f"(p90 {ratios[min(n - 1, int(n * 0.9))]:.1f}×). Зон за потолком: {over} из {n} "
            f"({over / n * 100:.0f}%) — там лишние корзины уточняют СЕТКУ, а не оценку.")
        say()

    hist: collections.Counter[str] = collections.Counter()
    for r in rows:
        v = max(r["val"], r["vah"])
        for name, (a, b) in (("[0,2)", (0, 2)), ("[2,5)", (2, 5)), ("[5,10)", (5, 10)),
                             ("[10,15)", (10, 15)), ("[15,20)", (15, 20)),
                             ("[20,30)", (20, 30)), ("[30,50)", (30, 50)),
                             ("[50,∞)", (50, 1e9))):
            if a <= v < b:
                hist[name] += 1
                break
    say("Гистограмма разброса value area (max(VAL,VAH)) — есть ли провал между режимами:")
    say()
    say("| корзина | зон |")
    say("|---|---|")
    for name in ("[0,2)", "[2,5)", "[5,10)", "[10,15)", "[15,20)",
                 "[20,30)", "[30,50)", "[50,∞)"):
        say(f"| {name}% | {hist.get(name, 0)} |")
    say()
    worst = sorted(rows, key=lambda r: -max(r["val"], r["vah"]))[:8]
    say("Худшие зоны:")
    say()
    say("| символ | ТФ | ПОК | VAL | VAH | добор низ/верх | ПЕРЕЗАКУП низ/верх | пол |")
    say("|---|---|---|---|---|---|---|---|")

    def _f(v: float | None) -> str:
        return f"{v:.1f}%" if v is not None else "—"

    for r in worst:
        say(f"| {r['symbol'].split('/')[0]} | {r['tf']} | {_f(r['poc'])} | "
            f"{_f(r['val'])} | {_f(r['vah'])} | {_f(r['zv_lo'])}/{_f(r['zv_hi'])} | "
            f"**{_f(r['pk_lo'])}/{_f(r['pk_hi'])}** | {_f(r.get('floor_pct'))} |")

    if args.write:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(
            "# Устойчивость VAL/VAH — 2026-07-27\n\n" + "\n".join(out) + "\n",
            encoding="utf-8",
        )
        print(f"\nотчёт: {REPORT}")


if __name__ == "__main__":
    asyncio.run(main())
