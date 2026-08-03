"""Открытый пункт 3.2: КАКОЙ ЧЛЕН доминирует в неустойчивости объёмного профиля —
грубость ИСХОДНОГО БАРА, ЧИСЛО КОРЗИН или КОНЦЫ СТРУКТУРНОГО ОКНА.

Замер на живых данных Binance USDⓈ-M. Ничего не правит в `hunt_core/`; переиспользует
`prizrak/poc.py::_structure_bars` и `features/volume_profile.py::volume_profile_levels`
как есть.

Что именно разделяется. Базовый замер (`docs/audit/value-area-stability-2026-07-27.md`)
крутил ЧИСЛО КОРЗИН и НАЧАЛО СЕТКИ вместе и получил медианный разброс ПОК 9.4% ширины
зоны. Но у профиля есть ещё два свободных параметра, которых тот замер не трогал:

  * ИСТОЧНИК — профиль строится по барам СВОЕГО тайм-фрейма зоны, и каждый бар размазывает
    объём РАВНОМЕРНО по всем корзинам между low и high. Измеренный медианный бар шире
    корзины в 13.6×, то есть 13 из 14 корзин под баром получают выдуманное распределение.
    Позиция Sierra Chart: точность даёт БОЛЕЕ МЕЛКИЙ ИСТОЧНИК, а не больше корзин.
  * ОКНО — где структура начинается и кончается. У крипты нет сессии, поэтому окно целиком
    наше, и оно не охраняется ничем.

Метод. На КАЖДОЙ зоне канон = профиль по барам своего ТФ, N=cfg.vp_buckets, origin=0.
Дальше каждый член крутится ПООДИНОЧКЕ при замороженных остальных, и меряется:

  * `dev`   — МЕДИАНА |возмущённое − канон| по возмущениям члена, в % ширины зоны.
              Главная величина: не зависит от того, сколько точек в сетке члена.
  * `spread`— max−min по сетке члена, та же нормировка. Сравнима с базовым замером,
              но РАСТЁТ с числом точек — поэтому вторична.

Требование I-6: неполное покрытие мелким источником (дыра в 1m-истории) НЕ подменяется
числом — доля объёма сверяется с ТФ-баром, и при расхождении >2% источник выбрасывается
и считается отдельно.

Запуск:
    uv run python scripts/probe_vp_term_dominance.py            # печать
    uv run python scripts/probe_vp_term_dominance.py --write    # + docs/audit/...
"""
from __future__ import annotations

import argparse
import asyncio
import bisect
import json
import pathlib
import statistics
import time

import ccxt.pro as ccxtpro
import polars as pl

from hunt_core.features.volume_profile import volume_profile_levels
from hunt_core.prizrak.accumulation import find_accumulation_zones
from hunt_core.prizrak.config import PrizrakConfig
from hunt_core.prizrak.poc import _POC_STABILITY_MAX_SPREAD, _structure_bars
from hunt_core.prizrak.setups import bars_from_ohlcv

REPORT = pathlib.Path("docs/audit/vp-term-dominance-2026-07-27.md")
CACHE = pathlib.Path("/private/tmp/claude-501/-Users-tonyaleksandrov-Documents-HUNTER/"
                     "32c07f00-aa2b-418b-940d-a4c03fd027de/scratchpad/vp_terms_raw.json")

_SYMBOLS = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT",
    "XRP/USDT:USDT", "DOGE/USDT:USDT", "LINK/USDT:USDT",
]
_TFS = ("15m", "1h", "4h")
_TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}
_FINE = ("15m", "5m", "1m")
# Лестница разрешения для проверки СХОДИМОСТИ (см. `convergence` в отчёте). Смысл: сам по себе
# «сдвиг при смене источника» ещё не значит, что мелкий источник ТОЧНЕЕ — он лишь ДРУГОЙ.
# Довод Sierra Chart проверяем так: если оценка сходится (шаг 1m→5m много меньше шага 5m→ТФ),
# то ряд имеет предел и крупный бар — смещённый край, а не равноправная альтернатива.
_LADDER = ("1m", "5m", "15m")

_BUCKETS = (40, 60, 90, 120)
_ORIGINS = (0.0, 0.25, 0.5)
_JITTER = (-2, -1, 0, 1, 2)
_COVERAGE_TOL = 0.02  # доля объёма, на которую мелкий источник может разойтись с ТФ-баром


# ---------------------------------------------------------------- данные

def _frame(bars: list[list[float]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "high": [float(b[2]) for b in bars],
            "low": [float(b[3]) for b in bars],
            "volume": [float(b[5]) for b in bars],
        }
    )


async def _fetch_range(ex, sym: str, tf: str, since: int, until: int) -> list[list[float]]:
    """Все бары ``tf`` на [since, until) — постранично, лимит 1500."""
    out: list[list[float]] = []
    cursor = since
    step = _TF_MS[tf]
    while cursor < until:
        chunk = await ex.fetch_ohlcv(sym, tf, since=cursor, limit=1500)
        if not chunk:
            break
        out.extend(b for b in chunk if since <= b[0] < until)
        nxt = chunk[-1][0] + step
        if nxt <= cursor:
            break
        cursor = nxt
        if len(chunk) < 1500 and chunk[-1][0] + step >= until:
            break
    seen: dict[int, list[float]] = {}
    for b in out:
        seen[int(b[0])] = b
    return [seen[k] for k in sorted(seen)]


def _merge(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged = [list(ordered[0])]
    for a, b in ordered[1:]:
        if a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


class FineStore:
    """Отсортированные мелкие бары символа + срез по времени."""

    def __init__(self) -> None:
        self._bars: dict[tuple[str, str], list[list[float]]] = {}

    def add(self, sym: str, tf: str, bars: list[list[float]]) -> None:
        key = (sym, tf)
        cur = self._bars.setdefault(key, [])
        cur.extend(bars)
        cur.sort(key=lambda b: b[0])
        dedup: dict[int, list[float]] = {int(b[0]): b for b in cur}
        self._bars[key] = [dedup[k] for k in sorted(dedup)]

    def slice(self, sym: str, tf: str, t0: int, t1: int) -> list[list[float]]:
        bars = self._bars.get((sym, tf))
        if not bars:
            return []
        keys = [b[0] for b in bars]
        i = bisect.bisect_left(keys, t0)
        j = bisect.bisect_left(keys, t1)
        return bars[i:j]


# ---------------------------------------------------------------- метрики

def _levels(bars: list[list[float]], *, buckets: int, origin: float, va: float
            ) -> tuple[float | None, float | None, float | None]:
    if len(bars) < 5:
        return None, None, None
    return volume_profile_levels(
        _frame(bars), buckets=buckets, value_area_pct=va, origin_shift=origin
    )


def _dev_spread(canon: float, perturbed: list[float], span: float
                ) -> tuple[float | None, float | None]:
    """(медиана |возмущение − канон|, max−min по канону+возмущениям) в % ширины зоны."""
    vals = [v for v in perturbed if v is not None]
    if not vals or span <= 0:
        return None, None
    dev = statistics.median(abs(v - canon) for v in vals) / span * 100.0
    allv = vals + [canon]
    return dev, (max(allv) - min(allv)) / span * 100.0


def _index_of_structure(bars: list[list[float]], struct: list[list[float]]
                        ) -> tuple[int, int] | None:
    """Индексы структурного подокна в исходном ряду — по меткам времени."""
    if not struct:
        return None
    keys = [b[0] for b in bars]
    try:
        i0 = keys.index(struct[0][0])
        i1 = keys.index(struct[-1][0])
    except ValueError:
        return None
    return i0, i1


# ---------------------------------------------------------------- замер зоны

def measure(sym: str, tf: str, bars: list[list[float]], zone: dict,
            store: FineStore, cfg: PrizrakConfig) -> dict | None:
    lo_box, hi_box = float(zone["lo"]), float(zone["hi"])
    span = hi_box - lo_box
    if span <= 0:
        return None
    struct = _structure_bars(bars, zone)
    if len(struct) < 5:
        return None
    idx = _index_of_structure(bars, struct)
    if idx is None:
        return None
    i0, i1 = idx
    va = cfg.vp_value_area_pct
    N = cfg.vp_buckets

    poc_c, vah_c, val_c = _levels(struct, buckets=N, origin=0.0, va=va)
    if poc_c is None or vah_c is None or val_c is None:
        return None
    canon = {"poc": poc_c, "vah": vah_c, "val": val_c}

    out: dict = {
        "symbol": sym, "tf": tf, "bars": len(struct),
        "lo": lo_box, "hi": hi_box, "span": span,
        "i0": i0, "i1": i1,
    }

    # ---- члены: собираем списки возмущённых значений по каждому уровню
    terms: dict[str, dict[str, list[float]]] = {}

    # (B) ЧИСЛО КОРЗИН — источник и окно заморожены
    tb: dict[str, list[float]] = {"poc": [], "vah": [], "val": []}
    bucket_pocs: dict[int, float] = {}
    for b in _BUCKETS:
        p, vh, vl = _levels(struct, buckets=b, origin=0.0, va=va)
        if p is None:
            continue
        bucket_pocs[b] = float(p)
        if b == N:
            continue
        tb["poc"].append(float(p))
        if vh is not None:
            tb["vah"].append(float(vh))
        if vl is not None:
            tb["val"].append(float(vl))
    terms["buckets"] = tb
    out["_bucket_pocs"] = bucket_pocs

    # (B') НАЧАЛО СЕТКИ — контроль, известен как инертный для гарда ПОКа
    to_: dict[str, list[float]] = {"poc": [], "vah": [], "val": []}
    for o in _ORIGINS:
        if o == 0.0:
            continue
        p, vh, vl = _levels(struct, buckets=N, origin=o, va=va)
        if p is None:
            continue
        to_["poc"].append(float(p))
        if vh is not None:
            to_["vah"].append(float(vh))
        if vl is not None:
            to_["val"].append(float(vl))
    terms["origin"] = to_

    # (C) КОНЦЫ ОКНА — ±1, ±2 бара с каждой стороны
    tw: dict[str, list[float]] = {"poc": [], "vah": [], "val": []}
    tw1: dict[str, list[float]] = {"poc": [], "vah": [], "val": []}
    clamped = 0
    for ds in _JITTER:
        for de in _JITTER:
            if ds == 0 and de == 0:
                continue
            a, b = i0 + ds, i1 + de
            if a < 0 or b >= len(bars):
                clamped += 1
                continue
            if b - a + 1 < 5:
                continue
            p, vh, vl = _levels(bars[a:b + 1], buckets=N, origin=0.0, va=va)
            if p is None:
                continue
            tw["poc"].append(float(p))
            if vh is not None:
                tw["vah"].append(float(vh))
            if vl is not None:
                tw["val"].append(float(vl))
            if abs(ds) <= 1 and abs(de) <= 1:
                tw1["poc"].append(float(p))
                if vh is not None:
                    tw1["vah"].append(float(vh))
                if vl is not None:
                    tw1["val"].append(float(vl))
    terms["window"] = tw
    terms["window1"] = tw1
    out["window_clamped"] = clamped

    # (A) ИСТОЧНИК — 5m и 1m на ТОМ ЖЕ отрезке времени, N и окно заморожены
    t0 = int(struct[0][0])
    t1 = int(struct[-1][0]) + _TF_MS[tf]
    vol_tf = sum(float(b[5]) for b in struct)
    ts: dict[str, list[float]] = {"poc": [], "vah": [], "val": []}
    src_detail: dict[str, dict] = {}
    fine_frames: dict[str, list[list[float]]] = {}
    src_levels: dict[str, list[float | None]] = {
        tf: [poc_c, vah_c, val_c],
    }
    # диапазон канонической сетки — контроль, что мелкий срез накрывает ТОТ ЖЕ отрезок цены
    out["canon_range"] = [min(float(b[3]) for b in struct), max(float(b[2]) for b in struct)]
    for src in _FINE:
        if src == tf:
            continue
        fine = store.slice(sym, src, t0, t1)
        expect = (t1 - t0) // _TF_MS[src]
        vol_fine = sum(float(b[5]) for b in fine)
        cover = (vol_fine / vol_tf) if vol_tf > 0 else 0.0
        ok = bool(fine) and len(fine) >= expect * 0.99 and abs(cover - 1.0) <= _COVERAGE_TOL
        src_detail[src] = {"n": len(fine), "expect": expect,
                           "vol_ratio": round(cover, 5), "ok": ok}
        if not ok:
            continue
        fine_frames[src] = fine
        src_detail[src]["range"] = [min(float(b[3]) for b in fine),
                                    max(float(b[2]) for b in fine)]
        p, vh, vl = _levels(fine, buckets=N, origin=0.0, va=va)
        if p is None:
            continue
        src_levels[src] = [float(p), float(vh) if vh is not None else None,
                           float(vl) if vl is not None else None]
        ts["poc"].append(float(p))
        if vh is not None:
            ts["vah"].append(float(vh))
        if vl is not None:
            ts["val"].append(float(vl))
    terms["source"] = ts
    out["source_detail"] = src_detail
    out["src_levels"] = src_levels

    # СХОДИМОСТЬ по лестнице разрешения: шаг между соседними источниками, в % ширины зоны.
    ladder = [s for s in (*_LADDER, tf) if s in src_levels]
    seen_ms: set[int] = set()
    ordered: list[str] = []
    for s in ladder:  # по возрастанию грубости, без повторов
        if _TF_MS[s] in seen_ms:
            continue
        seen_ms.add(_TF_MS[s])
        ordered.append(s)
    ordered.sort(key=lambda s: _TF_MS[s])
    out["ladder"] = ordered
    for li, (a, b) in enumerate(zip(ordered, ordered[1:], strict=False)):
        for k, lname in enumerate(("poc", "vah", "val")):
            va_, vb_ = src_levels[a][k], src_levels[b][k]
            if va_ is None or vb_ is None:
                continue
            out[f"step.{a}->{b}.{lname}"] = abs(va_ - vb_) / span * 100.0
        _ = li

    # 1m отдельно — сильнейшее уточнение источника
    ts1: dict[str, list[float]] = {"poc": [], "vah": [], "val": []}
    if "1m" in fine_frames:
        p, vh, vl = _levels(fine_frames["1m"], buckets=N, origin=0.0, va=va)
        if p is not None:
            ts1["poc"].append(float(p))
            if vh is not None:
                ts1["vah"].append(float(vh))
            if vl is not None:
                ts1["val"].append(float(vl))
    terms["source_1m"] = ts1

    for term, per_level in terms.items():
        for level, vals in per_level.items():
            dev, spread = _dev_spread(canon[level], vals, span)
            out[f"{term}.{level}.dev"] = dev
            out[f"{term}.{level}.spread"] = spread
            out[f"{term}.{level}.n"] = len(vals)

    # Снижает ли МЕЛКИЙ источник чувствительность к числу корзин?
    if "1m" in fine_frames:
        fine = fine_frames["1m"]
        p1, _, _ = _levels(fine, buckets=N, origin=0.0, va=va)
        if p1 is not None:
            fine_bucket: list[float] = []
            for b in _BUCKETS:
                if b == N:
                    continue
                p, _vh, _vl = _levels(fine, buckets=b, origin=0.0, va=va)
                if p is not None:
                    fine_bucket.append(float(p))
            d, s = _dev_spread(float(p1), fine_bucket, span)
            out["buckets_on_1m.poc.dev"] = d
            out["buckets_on_1m.poc.spread"] = s

    # геометрия для контекста
    prof_range = max(float(b[2]) for b in struct) - min(float(b[3]) for b in struct)
    ranges = sorted(float(b[2]) - float(b[3]) for b in struct)
    out["bar_over_bucket"] = (
        (ranges[len(ranges) // 2]) / (prof_range / N) if prof_range > 0 else None
    )
    out["bucket_pct_of_span"] = prof_range / N / span * 100.0 if span > 0 else None
    return out


# ---------------------------------------------------------------- прогон

async def collect(symbols: list[str]) -> list[dict]:
    cfg = PrizrakConfig()
    ex = ccxtpro.binanceusdm({"options": {"defaultType": "swap"}, "enableRateLimit": True})
    await ex.load_markets()
    rows: list[dict] = []
    t_start = time.time()  # noqa: TID251 — секундомер прогресса: разность двух локальных отметок
    try:
        for sym in symbols:
            tier: dict[str, list[list[float]]] = {}
            zones_by_tf: dict[str, list[dict]] = {}
            need: list[tuple[int, int]] = []
            for tf in _TFS:
                bars = await ex.fetch_ohlcv(sym, tf, limit=500)
                if len(bars) < 60:
                    continue
                tier[tf] = bars
                shaped = bars_from_ohlcv(bars)
                zs = find_accumulation_zones(shaped, tf=tf, cfg=cfg, max_zones=8)
                good = [z for z in zs if isinstance(z, dict) and z.get("lo") and z.get("hi")]
                zones_by_tf[tf] = good
                for z in good:
                    st = _structure_bars(bars, z)
                    if len(st) < 5:
                        continue
                    idx = _index_of_structure(bars, st)
                    if idx is None:
                        continue
                    # с запасом на джиттер окна ±2 бара
                    i0, i1 = idx
                    a = max(0, i0 - 2)
                    b = min(len(bars) - 1, i1 + 2)
                    need.append((int(bars[a][0]), int(bars[b][0]) + _TF_MS[tf]))
            merged = _merge(need)
            store = FineStore()
            for src in _FINE:
                total = sum(b - a for a, b in merged) // _TF_MS[src]
                print(f"  {sym} {src}: {len(merged)} интервалов, ~{total} баров …", flush=True)
                for a, b in merged:
                    got = await _fetch_range(ex, sym, src, a, b)
                    store.add(sym, src, got)
            for tf, zs in zones_by_tf.items():
                for z in zs:
                    m = measure(sym, tf, tier[tf], z, store, cfg)
                    if m:
                        rows.append(m)
            print(f"{sym}: зон промерено {len([r for r in rows if r['symbol'] == sym])} "
                  f"({time.time() - t_start:.0f}s)", flush=True)  # noqa: TID251 — секундомер прогресса: разность двух локальных отметок
    finally:
        await ex.close()
    return rows


def _stat(rows: list[dict], key: str) -> tuple[int, float, float, float] | None:
    vs = sorted(r[key] for r in rows if r.get(key) is not None)
    if not vs:
        return None
    n = len(vs)
    return n, vs[n // 2], vs[min(n - 1, int(n * 0.9))], vs[-1]


def report(rows: list[dict], write: bool) -> None:
    out: list[str] = []

    def say(line: str = "") -> None:
        print(line)
        out.append(line)

    cfg = PrizrakConfig()
    say(f"зон промерено: **{len(rows)}** ({len(_SYMBOLS)} символов × {len(_TFS)} ТФ), "
        f"канон = профиль по барам своего ТФ, N={cfg.vp_buckets}, origin=0")
    say()

    src_ok = sum(1 for r in rows if (r.get("source_detail") or {}).get("1m", {}).get("ok"))
    src5_ok = sum(1 for r in rows if (r.get("source_detail") or {}).get("5m", {}).get("ok"))
    say(f"Покрытие мелким источником (проверка I-6, объём сверен с ТФ-баром ±"
        f"{_COVERAGE_TOL:.0%}): 1m годен на {src_ok} зонах, 5m на {src5_ok}. "
        "Негодные из соответствующего члена ИСКЛЮЧЕНЫ, не заменены числом.")
    say()

    # Контроль против САМОГО ОПАСНОГО артефакта этого замера: сдвиг среза по времени на один
    # бар дал бы «эффект источника» из ничего. Диапазон [min low, max high] у мелкого среза
    # ОБЯЗАН совпадать с каноническим — 4h-хай это максимум своих 1m-хаёв. Расходится —
    # значит меряется не разрешение, а разные отрезки времени.
    worst_range = 0.0
    checked = 0
    for r in rows:
        cr = r.get("canon_range")
        if not cr:
            continue
        for src, det in (r.get("source_detail") or {}).items():
            rng = det.get("range")
            if not rng:
                continue
            checked += 1
            denom = cr[1] - cr[0]
            if denom > 0:
                worst_range = max(worst_range,
                                  abs(rng[0] - cr[0]) / denom, abs(rng[1] - cr[1]) / denom)
    say(f"Контроль среза по времени: диапазон [min low, max high] мелкого источника сверен с "
        f"каноническим на {checked} парах, худшее расхождение "
        f"**{worst_range * 100:.4f}%** от диапазона профиля. Ноль означает, что сравниваются "
        "РОВНО те же бары в РОВНО той же ценовой сетке — эффект источника не может быть "
        "артефактом сдвига окна на бар.")
    say()

    terms = [
        ("source", "ИСТОЧНИК (5m+1m vs свой ТФ)"),
        ("source_1m", "  из них только 1m"),
        ("buckets", "ЧИСЛО КОРЗИН (40/90/120 vs 60)"),
        ("window", "ОКНО ±1/±2 бара (24 сдвига)"),
        ("window1", "  из них только ±1 (8 сдвигов)"),
        ("origin", "НАЧАЛО СЕТКИ (0.25/0.5)"),
    ]
    for level, title in (("poc", "ПОК"), ("val", "VAL"), ("vah", "VAH")):
        say(f"### {title} — медиана |смещения| от канона, в % ШИРИНЫ ЗОНЫ")
        say()
        say("| член | зон | медиана dev | p90 dev | макс dev | медиана spread |")
        say("|---|---|---|---|---|---|")
        ranked = []
        for term, name in terms:
            s = _stat(rows, f"{term}.{level}.dev")
            sp = _stat(rows, f"{term}.{level}.spread")
            if not s:
                continue
            ranked.append((name, s, sp))
        for name, s, sp in sorted(ranked, key=lambda x: -x[1][1]):
            n, med, p90, mx = s
            spm = f"{sp[1]:.1f}%" if sp else "—"
            say(f"| {name} | {n} | **{med:.1f}%** | {p90:.1f}% | {mx:.1f}% | {spm} |")
        say()

    # попарное сравнение НА ОДНОЙ зоне — кто больше
    say("### Попарно на ОДНОЙ И ТОЙ ЖЕ зоне (кто сдвигает ПОК сильнее)")
    say()
    say("| пара | A > B | B > A | ничья |")
    say("|---|---|---|---|")
    for a, b in (("window", "buckets"), ("window", "source"), ("source", "buckets"),
                 ("buckets", "origin")):
        ka, kb = f"{a}.poc.dev", f"{b}.poc.dev"
        both = [(r[ka], r[kb]) for r in rows
                if r.get(ka) is not None and r.get(kb) is not None]
        if not both:
            continue
        agt = sum(1 for x, y in both if x > y)
        bgt = sum(1 for x, y in both if y > x)
        tie = len(both) - agt - bgt
        say(f"| {a} vs {b} (n={len(both)}) | {agt} | {bgt} | {tie} |")
    say()

    # ---- СХОДИМОСТЬ: мелкий источник ТОЧНЕЕ или просто ДРУГОЙ?
    say("### Сходимость по лестнице разрешения 1m → 5m → 15m → свой ТФ")
    say()
    say("Смысл проверки. «Профиль сдвинулся при смене источника» само по себе не доказывает, "
        "что мелкий источник ТОЧНЕЕ — он лишь ДРУГОЙ. Довод Sierra Chart проверяется "
        "сходимостью: если шаг 1m→5m много меньше шага 15m→ТФ, ряд имеет предел, и крупный "
        "бар — смещённый КРАЙ ряда, а не равноправная альтернатива. Если же все шаги "
        "одного порядка, это просто шум, и «более мелкий источник» ничего не покупает.")
    say()
    say("| шаг | зон | медиана \\|Δ ПОК\\| | медиана \\|Δ VAL\\| | медиана \\|Δ VAH\\| |")
    say("|---|---|---|---|---|")
    steps = [("1m", "5m"), ("5m", "15m"), ("15m", "1h"), ("15m", "4h"), ("1h", "4h")]
    for a, b in steps:
        cells, n = [], 0
        for lname in ("poc", "val", "vah"):
            vs = [r[f"step.{a}->{b}.{lname}"] for r in rows
                  if r.get(f"step.{a}->{b}.{lname}") is not None]
            n = max(n, len(vs))
            cells.append(f"{statistics.median(vs):.1f}%" if vs else "—")
        if n:
            say(f"| {a} → {b} | {n} | " + " | ".join(cells) + " |")
    say()
    # попарно на одной зоне: правда ли последний шаг (к своему ТФ) — самый большой
    for tf in ("1h", "4h"):
        pair = [(r["step.1m->5m.poc"], r[f"step.15m->{tf}.poc"]) for r in rows
                if r["tf"] == tf and r.get("step.1m->5m.poc") is not None
                and r.get(f"step.15m->{tf}.poc") is not None]
        if not pair:
            continue
        bigger = sum(1 for x, y in pair if y > x)
        say(f"На зонах ТФ={tf} (n={len(pair)}): шаг 15m→{tf} больше шага 1m→5m на "
            f"**{bigger} из {len(pair)}** зон "
            f"(медианы {statistics.median(y for _, y in pair):.1f}% против "
            f"{statistics.median(x for x, _ in pair):.1f}%).")
    say()

    # снижает ли мелкий источник чувствительность к корзинам
    a = _stat(rows, "buckets.poc.dev")
    b = _stat(rows, "buckets_on_1m.poc.dev")
    if a and b:
        paired = [(r["buckets.poc.dev"], r["buckets_on_1m.poc.dev"]) for r in rows
                  if r.get("buckets.poc.dev") is not None
                  and r.get("buckets_on_1m.poc.dev") is not None]
        if paired:
            lower = sum(1 for x, y in paired if y < x)
            say(f"**Лечит ли мелкий источник чувствительность к корзинам?** На {len(paired)} "
                f"зонах с годным 1m: медиана bucket-dev по ТФ-барам "
                f"{statistics.median(x for x, _ in paired):.1f}%, по 1m-барам "
                f"{statistics.median(y for _, y in paired):.1f}%; ниже стало на "
                f"{lower} из {len(paired)} зон.")
            say()

    # разбивка по ТФ
    say("### По тайм-фреймам — медиана dev ПОКа")
    say()
    say("| ТФ | зон | источник | корзины | окно ±2 | начало |")
    say("|---|---|---|---|---|---|")
    for tf in _TFS:
        sub = [r for r in rows if r["tf"] == tf]
        if not sub:
            continue
        cells = []
        for term in ("source", "buckets", "window", "origin"):
            vs = [r[f"{term}.poc.dev"] for r in sub if r.get(f"{term}.poc.dev") is not None]
            cells.append(f"{statistics.median(vs):.1f}%" if vs else "—")
        say(f"| {tf} | {len(sub)} | " + " | ".join(cells) + " |")
    say()

    # ---- АБЛЯЦИЯ пробы корзин
    say("### Абляция пробы `_POC_STABILITY_BUCKETS` — что дают N=90 и N=120")
    say()
    thr = _POC_STABILITY_MAX_SPREAD
    subsets = {
        "(40,60) — текущая минус 90,120": (40, 60),
        "(40,60,90)": (40, 60, 90),
        "(40,60,120)": (40, 60, 120),
        "(40,60,90,120) — как в коде": (40, 60, 90, 120),
    }
    base_flag: dict[str, set[int]] = {}
    say("| проба | зон измеримо | неустойчивых (>%.0f%%) | добавила к (40,60) |" % thr)
    say("|---|---|---|---|")
    ref: set[int] = set()
    for i, (name, subset) in enumerate(subsets.items()):
        flagged: set[int] = set()
        measurable = 0
        for j, r in enumerate(rows):
            bp = r.get("_bucket_pocs") or {}
            vals = [bp[str(b)] if str(b) in bp else bp.get(b) for b in subset]
            vals = [v for v in vals if v is not None]
            if len(vals) < 2:
                continue
            measurable += 1
            if (max(vals) - min(vals)) / r["span"] * 100.0 > thr:
                flagged.add(j)
        base_flag[name] = flagged
        if i == 0:
            ref = flagged
            say(f"| {name} | {measurable} | {len(flagged)} | — |")
        else:
            say(f"| {name} | {measurable} | {len(flagged)} | +{len(flagged - ref)} |")
    say()

    # окно как гард: сколько зон окно объявило бы неустойчивыми
    say("### Если бы порог 15% применялся к ДРУГИМ членам (тот же смысл, тот же порог)")
    say()
    say("| член | зон | spread > 15% |")
    say("|---|---|---|")
    for term, name in terms:
        vs = [r[f"{term}.poc.spread"] for r in rows
              if r.get(f"{term}.poc.spread") is not None]
        if not vs:
            continue
        over = sum(1 for v in vs if v > thr)
        say(f"| {name} | {len(vs)} | {over} ({over / len(vs) * 100:.0f}%) |")
    say()

    br = _stat(rows, "bar_over_bucket")
    if br:
        say(f"Контроль геометрии: медианный бар шире корзины в {br[1]:.1f}× "
            f"(p90 {br[2]:.1f}×) — тот же потолок разрешения, что в базовом замере.")
        say()

    worst = sorted((r for r in rows if r.get("window.poc.dev") is not None),
                   key=lambda r: -r["window.poc.dev"])[:10]
    say("### Худшие по ОКНУ")
    say()
    say("| символ | ТФ | баров | источник | корзины | окно ±2 | окно ±1 |")
    say("|---|---|---|---|---|---|---|")
    for r in worst:
        def f(k: str, r: dict = r) -> str:  # noqa: B006 — r связан значением, см. ниже
            # `r` связан значением по умолчанию, а не захвачен замыканием (ruff B023):
            # иначе все строки таблицы напечатались бы по последнему элементу `worst`,
            # как только вызов `f` окажется отложенным. Сейчас он синхронный, но
            # правильность отчёта не должна держаться на порядке исполнения.
            v = r.get(k)
            return f"{v:.1f}%" if v is not None else "—"
        say(f"| {r['symbol'].split('/')[0]} | {r['tf']} | {r['bars']} | "
            f"{f('source.poc.dev')} | {f('buckets.poc.dev')} | "
            f"**{f('window.poc.dev')}** | {f('window1.poc.dev')} |")

    if write:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(
            "# Что доминирует в неустойчивости объёмного профиля — 2026-07-27\n\n"
            + "\n".join(out) + "\n", encoding="utf-8")
        print(f"\nотчёт: {REPORT}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--cached", action="store_true", help="перечитать сырые числа из кэша")
    ap.add_argument("--symbols", default="", help="через запятую, для дымовой пробы")
    args = ap.parse_args()
    if args.cached and CACHE.exists():
        rows = json.loads(CACHE.read_text())
    else:
        rows = await collect([s for s in args.symbols.split(",") if s] or _SYMBOLS)
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(rows))
    if not rows:
        print("зон не найдено — замерять нечего")
        return
    report(rows, args.write)


if __name__ == "__main__":
    asyncio.run(main())
