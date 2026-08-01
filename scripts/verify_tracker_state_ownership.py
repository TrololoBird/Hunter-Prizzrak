"""Владение состоянием трекера: теряет ли флаш чужие правки и затирает ли объявленную сделку.

ЗАЧЕМ. Аудит архитектуры 2026-08-01 назвал это корневой причиной R3: у состояния сделки нет
владельца — три независимых писателя (`_cycle_tick`, `_cycle_loop`, `_cycle_reconcile`) делают
`load → mutate → save` в один файл без замка. Это единственный класс из аудита, который
**уничтожает уже объявленные данные**, а не искажает их.

Проверяются ДВА утверждения, и оба на настоящих функциях, а не на их пересказе:

1. **Слияние не теряет чужие символы.** `data/lake.py::_merge_tracker_state` до правки
   заменял всё, кроме `signals`/`followup_sent`/`closed_history`, копией из буфера — а живое
   состояние лежит ровно в остальных ключах (замер: `zone_registry` 20, `zone_events` 20,
   `zone_watch` 18, `zone_announced` 6 при нуле сигналов).

2. **Повторная регистрация не затирает живую сделку.** `track/tracker.py::register_signal_open`
   заканчивался безусловным присваиванием: второй вызов по тому же `SYMBOL:direction` молча
   заменял запись — без `close_signal`, без архива, без лога.

⚠ ПОЧЕМУ ЗДЕСЬ НЕТ ЖИВОГО CCXT. Это единственный верификатор проекта, которому биржа не нужна
по существу: предмет проверки — арифметика слияния словарей и гард перезаписи, а не рыночные
числа. Данные берутся из НАСТОЯЩЕГО `data/hunt_signal_state.json`, если он есть, — то есть
проверка идёт на форме, которую писал боевой прогон, а не на выдуманной.

    uv run python scripts/verify_tracker_state_ownership.py
"""
from __future__ import annotations

import sys
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hunt_core import serde  # noqa: E402
from hunt_core.data.lake import _merge_tracker_state  # noqa: E402
from hunt_core.paths import SIGNAL_STATE  # noqa: E402
from hunt_core.track.tracker import register_signal_open  # noqa: E402

FAIL: list[str] = []
NOTES: list[str] = []


def _live_state() -> dict[str, Any]:
    """Настоящее состояние с диска; пустое — если файла нет (это тоже данные)."""
    path = SIGNAL_STATE
    if not path.is_file():
        NOTES.append(f"{path.name} отсутствует — форма взята минимальная, а не боевая")
        return {}
    try:
        raw = serde.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — битый файл не приговор проверке
        NOTES.append(f"{path.name} не разобран ({exc.__class__.__name__}) — форма минимальная")
        return {}
    return raw if isinstance(raw, dict) else {}


def check_merge_keeps_other_writers(live: dict[str, Any]) -> None:
    """Символ, добавленный ДРУГИМ писателем после загрузки буфера, обязан пережить флаш."""
    on_disk = deepcopy(live) or {"zone_registry": {}, "zone_events": {}}
    buffered = deepcopy(on_disk)  # копия, снятая в начале окна писателя

    # Другой писатель добавил свой символ уже ПОСЛЕ того, как буфер был снят.
    for key in ("zone_registry", "zone_events", "zone_watch", "zone_announced"):
        on_disk.setdefault(key, {})
        buffered.setdefault(key, {})
        on_disk[key]["__OTHER_WRITER__"] = {"marker": key}
    # А наш писатель тронул свой.
    buffered["zone_registry"]["__OUR_WRITER__"] = {"marker": "ours"}

    merged = _merge_tracker_state(on_disk, buffered)

    for key in ("zone_registry", "zone_events", "zone_watch", "zone_announced"):
        got = (merged.get(key) or {})
        if "__OTHER_WRITER__" not in got:
            FAIL.append(f"слияние ПОТЕРЯЛО символ другого писателя в '{key}'")
    if "__OUR_WRITER__" not in (merged.get("zone_registry") or {}):
        FAIL.append("слияние потеряло символ СВОЕГО писателя в 'zone_registry'")

    # И не должно воскрешать то, чего не было ни там, ни там.
    if "__GHOST__" in (merged.get("zone_registry") or {}):
        FAIL.append("слияние выдумало символ, которого не было ни на диске, ни в буфере")

    # Живые символы обязаны остаться на месте — их никто не трогал.
    for key in ("zone_registry", "zone_events", "zone_watch", "zone_announced"):
        before = {k for k in (live.get(key) or {})}
        after = {k for k in (merged.get(key) or {})}
        lost = before - after
        if lost:
            FAIL.append(f"слияние потеряло живые символы в '{key}': {sorted(lost)[:5]}")
    print(f"  слияние: ключей на выходе {len(merged)}, "
          f"zone_registry {len(merged.get('zone_registry') or {})}")


def check_register_refuses_occupied() -> None:
    """Второй вызов по занятому направлению не должен стирать первую сделку."""
    now = datetime.now(UTC)
    state: dict[str, Any] = {}
    setup = {
        "direction": "long",
        "entry_zone": [100.0, 101.0],
        "stop_loss": 95.0,
        "tp1": 110.0,
        "tp2": 120.0,
        "phase": "zone_perezakup",
    }
    register_signal_open(
        state, symbol="TESTUSDT", direction="long", price=100.5,
        setup=setup, lifecycle={}, now=now,
    )
    first = deepcopy((state.get("signals") or {}).get("TESTUSDT:long"))
    if not isinstance(first, dict):
        FAIL.append("первая регистрация не создала запись — проверка невозможна")
        return

    # Вторая попытка по тому же направлению, с ДРУГОЙ геометрией.
    second_setup = {**setup, "entry_zone": [200.0, 201.0], "stop_loss": 190.0, "tp1": 220.0}
    register_signal_open(
        state, symbol="TESTUSDT", direction="long", price=200.5,
        setup=second_setup, lifecycle={}, now=now + timedelta(minutes=5),
    )
    after = (state.get("signals") or {}).get("TESTUSDT:long")

    if not isinstance(after, dict):
        FAIL.append("после второй регистрации запись пропала совсем")
        return
    if after.get("stop_loss") != first.get("stop_loss"):
        FAIL.append(
            f"живая сделка ЗАТЁРТА: стоп был {first.get('stop_loss')}, стал {after.get('stop_loss')}"
        )
    if after.get("opened_at") != first.get("opened_at"):
        FAIL.append("живая сделка затёрта: сменился opened_at")
    if len(state.get("closed_history") or []) > 0:
        FAIL.append("отказ не должен архивировать первую сделку — она ещё живая")
    print(f"  перезапись: стоп остался {after.get('stop_loss')}, "
          f"архив пуст: {not (state.get('closed_history') or [])}")

    # Противоположное направление ДОЛЖНО проходить — гард не обязан его блокировать.
    register_signal_open(
        state, symbol="TESTUSDT", direction="short", price=200.5,
        setup={**setup, "direction": "short", "stop_loss": 210.0, "tp1": 180.0},
        lifecycle={}, now=now + timedelta(minutes=6),
    )
    if "TESTUSDT:short" not in (state.get("signals") or {}):
        FAIL.append("гард заблокировал ПРОТИВОПОЛОЖНОЕ направление — он слишком широк")
    else:
        print("  противоположное направление проходит — гард не переусердствовал")


def check_armed_expires() -> None:
    """Отдыхающий лимит (`armed`) обязан истекать по TTL, а не висеть вечно.

    ⚠ САМЫЙ ОПАСНЫЙ ИЗ НОВЫХ СЦЕНАРИЕВ. Гард занятого направления в `register_signal_open`
    отказывает новому сигналу, пока старый жив. Значит запись, которую нечем снять,
    превращается из безобидной утечки состояния в ВЕЧНУЮ ПРОБКУ на канале. До правки
    `_evaluate_levels.py` выходил на `armed` РАНЬШЕ проверки orphan-TTL, то есть снять
    такую запись было физически нечем.
    """
    from hunt_core.track._evaluate_levels import evaluate_levels

    now = datetime.now(UTC)
    stale = now - timedelta(hours=400)  # заведомо за любым TTL (пол для лонга — 48 ч)
    state: dict[str, Any] = {
        "signals": {
            "TESTUSDT:long": {
                "symbol": "TESTUSDT",
                "direction": "long",
                "status": "active",
                "delivery_tier": "armed",
                "opened_at": stale.isoformat(),
                "last_reconcile_ts": stale.isoformat(),
                "entry_lo": 100.0,
                "entry_hi": 101.0,
                "entry_zone": [100.0, 101.0],
                "stop_loss": 95.0,
                "tp1": 110.0,
                "extreme_hi": 100.5,
                "extreme_lo": 100.5,
            }
        }
    }
    evaluate_levels(
        state, symbol="TESTUSDT", direction="long",
        price=100.5, hi=100.6, lo=100.4, ts=now,
    )
    sig = (state.get("signals") or {}).get("TESTUSDT:long") or {}
    status = str(sig.get("status") or "")
    if status == "active":
        FAIL.append(
            "висящий armed НЕ истёк по TTL — он заблокирует направление навсегда "
            "(гард занятого направления не даст открыть новый сигнал)"
        )
    else:
        print(f"  armed возрастом 400 ч закрыт: status={status}, "
              f"причина={sig.get('close_reason')}")

    # И контроль обратного: СВЕЖИЙ armed трогать нельзя — позиции ещё нет.
    fresh_state: dict[str, Any] = deepcopy(state)
    fresh = fresh_state["signals"]["TESTUSDT:long"]
    fresh.update({
        "status": "active", "close_reason": None,
        "opened_at": now.isoformat(), "last_reconcile_ts": now.isoformat(),
    })
    evaluate_levels(
        fresh_state, symbol="TESTUSDT", direction="long",
        price=100.5, hi=112.0, lo=90.0, ts=now,  # цена «прошла» и стоп, и цель
    )
    got = (fresh_state.get("signals") or {}).get("TESTUSDT:long") or {}
    if str(got.get("status") or "") != "active":
        FAIL.append(
            f"СВЕЖИЙ armed закрыт как {got.get('close_reason')} — машина SL/TP отработала "
            "по сделке, которой не было (лимит не залился)"
        )
    else:
        print("  свежий armed не тронут машиной SL/TP — позиции ещё нет")


def main() -> int:
    live = _live_state()
    print("=== 1. слияние состояния не теряет правки других писателей")
    check_merge_keeps_other_writers(live)
    print("\n=== 2. регистрация не затирает уже объявленную сделку")
    check_register_refuses_occupied()
    print("\n=== 3. висящий armed истекает и не превращается в вечную пробку")
    check_armed_expires()

    if NOTES:
        print("\nоговорки:")
        for n in NOTES:
            print("   ", n)
    if FAIL:
        print(f"\nНАРУШЕНИЙ: {len(FAIL)}")
        for f in FAIL:
            print("   ", f)
        return 1
    print("\nВЛАДЕНИЕ СОСТОЯНИЕМ КОРРЕКТНО: чужие правки переживают флаш, "
          "объявленная сделка не затирается, противоположное направление не блокируется")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
