"""Per-tick data-plane truth table — поле / источник / возраст, БЕЗ фабрикации.

Зачем модуль вообще есть. Самый дорогой класс инцидентов здесь — не падение, а тихий блэкаут:
кадр замерзает, бот продолжает тикать, сигналы просто перестают формироваться. Диагностируется он
одним вопросом — «сколько лет данным, из которых мы посчитали вот это». Этот файл отвечает на него
построчно и складывает ответ в ``data/data_plane_audit.jsonl``.

⚠ ПОЧЕМУ ПЕРЕПИСАН 2026-07-26. Прежняя редакция брала возрасты из
``client.snapshot_rest_cache_ages(symbol)`` и ``pack["_rest_cache_ages"]``, а источники — из
``prepared.*_source`` и ``ws_snap``. Ни одного из этих четырёх аргументов вызывающий
(``tick_diagnostics.append_tick_diagnostics``) не передаёт с момента переписывания транспорта
(``5ba0fea``, 2026-07-19): он зовёт с одной строкой. Замер по 2000 последних живых записей:

    поле                    n     age_s=None   value=None   источник
    oi                   2000         2000         2000     rest_fetch_open_interest
    funding_rate         2000         2000         2000     rest_fetch_funding_rate
    1h_closed            2000         2000            —     rest_klines
    …и так все 16 полей

То есть **колонка возраста была пуста у 100% полей, а колонка источника при этом уверенно врала**:
``rest_fetch_funding_rate`` печаталось для значения, которого в строке нет вовсе (ключи
``live_funding_rate`` / ``funding_live`` / ``row["timeframes"]`` — сироты, их продюсер ушёл вместе
с легаси-транспортом). Инструмент диагностики блэкаутов сам был нарушением I-6, и агенты,
пытавшиеся им диагностировать, получали правдоподобную пустышку (память
``pinned-4h-stale-blackout-rest-starvation`` фиксирует ровно это: «смотрите живой лог, не аудит»).

Как устроено теперь. Единственный источник возраста — штампы движка,
``engine/api.py::plane_ages`` (ADR-0004 E7), которые тик кладёт в строку как ``plane_ages``;
единственный источник «плана нет» — ``view.not_ready`` (в строке — ``data_violations``).
Значения читаются по ТЕМ ЖЕ именам, под которыми их пишет ``maps/engine.py::derive_map_features``.
Ничего не выводится по догадке: план, которого движок не проштамповал, получает
``source="absent"`` и ``age_s=None`` — и это ЧЕСТНЫЙ ответ, а не подставленный REST-ярлык.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from hunt_core import serde
from hunt_core.paths import DATA_PLANE_AUDIT_JSONL

# ⚠ Здесь стояла таблица `_REFRESH_HINT_S` + множитель `_STALE_MULT` — «во сколько раз возраст
# должен превысить ориентир, чтобы считаться протухшим». Снята сразу после ревью, не дожив до
# коммита, и по двум причинам, каждой из которых достаточно:
#
# 1. **Комментарий врал.** Значения были подписаны как «периоды опроса движка (`engine/params.py`)»,
#    но пять из девяти ими не были: `oi 600` при `FUTURES_DATA_POLL_S=300`, `ticker 60` при
#    `FRESH_TICKER_S=10`, `mark 5` при `FRESH_MARK_S=15`, `trades 30` при watchdog'е 60,
#    `funding 300` при каденции сеттла 8 ч. То есть это были «разумные значения» — ровно то, что
#    запрещает I-7.
# 2. **Ветка почти инертна, а где не инертна — вредна.** Все девять планов входят в `required`
#    (`view/build.py`), поэтому план старше своего `PlaneStamp.bound_ms` УЖЕ попадает в
#    `not_ready` и получает `stale` по настоящей причине от движка. Чтобы hint что-то добавил,
#    нужно `hint*3 < bound_ms`; это выполнялось ровно для `funding` — и там правило сработало бы
#    ПРОТИВ контракта движка, пометив 20-минутный фандинг протухшим при 8-часовой каденции.
#
# Единственный источник свежести — бонды самого движка через `not_ready`. Второй, параллельный
# набор порогов в диагностике неизбежно разъезжается с первым и начинает врать громче, чем молчит.

# Скаляры, которые доезжают до карточки и до гейтов: (поле в аудите, реальный ключ в
# `row["market"]`, план-источник). Третий элемент — `None`, когда значение считается МИМО планов:
# тогда возраст плана к нему не относится, и подставлять его было бы новой фабрикацией.
#
# ⚠ Продюсеров ДВА, и это ловушка. `map_book_imbalance_1pct` / `map_vp_poc` / `liq_*` пишет
# `maps/engine.py::derive_map_features`, а `map_oi_z` / `map_funding_rate` / `map_basis_pct` /
# `map_ws_cvd` — `maps/feed.py::build_map_bundle` в `bundle.extra`, который engine лишь вливает
# (`out.update(bundle.extra)`). Проверять имя надо в ОБОИХ файлах: указание на один уже привело
# к дефекту (`extra` собирался литералом, и `map_basis_pct` приходил как None у всех символов).
_VALUE_FIELDS: tuple[tuple[str, str, str | None], ...] = (
    ("funding_rate", "map_funding_rate", "funding"),
    # `oi_z` считает `runtime/native_assembly.py::_fetch_oi_bars` собственным REST-запросом
    # (`fapiDataGetOpenInterestHist`, TTL 300 с) МИМО плоскости планов — штампа у него нет.
    # Печатать здесь возраст плана `oi` значило бы выдать 9-секундную свежесть за пятиминутный кэш.
    ("oi_z", "map_oi_z", None),
    ("basis_pct", "map_basis_pct", "basis"),
    ("ws_cvd", "map_ws_cvd", "trades"),
    ("book_imbalance_1pct", "map_book_imbalance_1pct", "book"),
    # ⚠ План — 1h, НЕ 15m: `maps/volume_profile.py` выбирает первичным профиль периода "1h"
    # (`next((p for p in profiles if p.period == "1h"), profiles[0])`). Привязка к 15m печатала бы
    # свежий возраст для ПОК, посчитанного по часовому кадру, — то есть ровно ту сигнатуру
    # `stale-htf-cache-trap`, которую этот инструмент и написан ловить.
    ("vp_poc", "map_vp_poc", "kline.1h"),
    # ⚠ Здесь НЕЛЬЗЯ брать `liq_forward_weight`: он равен `liq_forward_confidence ×
    # forward_blend_ratio`, а при нуле реальных событий сама confidence равна тому же
    # `forward_blend_ratio` — то есть поле вырождается в `0.35²` из TOML (замер: 0.122 у всех 7
    # живых строк при `liq_realized_events=0`). Число из конфига в колонке «значение» хуже
    # фантома: фантом виден как None, а это выглядит измерением. Берём счётчик РЕАЛЬНЫХ событий —
    # у него ноль честный.
    ("liq_realized_events", "liq_realized_events", "liq"),
)


def data_plane_audit_enabled() -> bool:
    return os.getenv("HUNT_DATA_PLANE_AUDIT", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _field_entry(
    *,
    field: str,
    source: str,
    age_s: float | None,
    refresh_hint_s: int | None = None,
    stale: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "field": field,
        "source": source,
        "age_s": round(age_s, 2) if age_s is not None else None,
    }
    if refresh_hint_s is not None:
        row["ttl_hint_s"] = refresh_hint_s
    if stale:
        row["stale"] = True
    if extra:
        row.update(extra)
    return row


def _plane_reasons(violations: Any) -> dict[str, str]:
    """``["kline.4h: stale 40s>36s", …]`` → ``{"kline.4h": "stale 40s>36s"}`` (fail-loud причины)."""
    out: dict[str, str] = {}
    if not isinstance(violations, (list, tuple)):
        return out
    for item in violations:
        text = str(item)
        plane, _, reason = text.partition(":")
        # `engine/api.py::snapshot` эмитит ещё одну форму — `f"{symbol}: not tracked"`, а символ
        # ccxt-unified и САМ содержит двоеточие (`BTC/USDT:USDT`). Резать по первому двоеточию
        # дало бы фиктивный план `"BTC/USDT"`. Сегодня недостижимо (у неотслеживаемого символа
        # нет цены → `build_market_view` вернёт None → тик пишет `_error_row`, а он до аудита не
        # доходит), но это совпадение чужого потока управления, а не свойство функции.
        if "/" in plane:
            continue
        if reason:
            out[plane.strip()] = reason.strip()
    return out


def build_data_plane_audit(row: dict[str, Any]) -> dict[str, Any]:
    """Собрать запись аудита из НАТИВНОЙ строки тика: план → источник → настоящий возраст."""
    symbol = str(row.get("symbol") or "").upper()
    _market = row.get("market")
    market = _market if isinstance(_market, dict) else {}
    _ages = row.get("plane_ages")
    ages: dict[str, float] = {}
    if isinstance(_ages, dict):
        for name, value in _ages.items():
            try:
                ages[str(name)] = float(value)
            except (TypeError, ValueError):
                continue
    reasons = _plane_reasons(row.get("data_violations"))

    plane_fields: list[dict[str, Any]] = []
    value_fields: list[dict[str, Any]] = []

    # 1. Планы: возраст со штампа движка. План, которого нет в `plane_ages`, но который движок
    #    назвал в `not_ready`, получает `source="absent"` — это ответ, а не пропуск.
    for plane in sorted(set(ages) | set(reasons)):
        age = ages.get(plane)
        reason = reasons.get(plane)
        plane_fields.append(
            _field_entry(
                field=plane,
                source="engine_plane" if age is not None else "absent",
                age_s=age,
                # `stale` — исключительно вердикт движка (`PlaneStamp.stale_by`), а не наша
                # арифметика над возрастом: бонд знает только движок, см. NB у снятой таблицы.
                stale=bool(reason and reason.startswith("stale")),
                extra={"not_ready": reason} if reason else None,
            )
        )

    # 2. Скаляры: значение по РЕАЛЬНОМУ ключу + возраст плана, из которого оно посчитано.
    #    `present` отделяет «плана нет» от «план есть, значение ноль» — ровно та граница, на
    #    которой ломается I-6. Значение, посчитанное мимо планов (`plane is None`), возраста НЕ
    #    получает: подставить туда чужой было бы новой фабрикацией того же рода.
    for field_name, market_key, src_plane in _VALUE_FIELDS:
        age = ages.get(src_plane) if src_plane is not None else None
        source: str
        if src_plane is None:
            source = "unstamped_cache"
        elif age is not None:
            source = f"engine_plane:{src_plane}"
        else:
            source = "absent"
        value_fields.append(
            _field_entry(
                field=field_name,
                source=source,
                age_s=age,
                extra={"value": market.get(market_key), "present": market_key in market},
            )
        )
    fields = plane_fields + value_fields

    # 3. Свежесть самого тика — уже посчитана нативно (`native_producers.freshness_native`).
    _fresh = row.get("freshness")
    fresh = _fresh if isinstance(_fresh, dict) else {}

    # ⚠ Сводка считается ТОЛЬКО по планам. Скаляры копируют возраст своего плана, поэтому,
    # попадая в ту же выборку, они дублируют его и смещают медиану к тем планам, у которых
    # больше производных; а `absent` у скаляра означает «нет значения», а не «нет плана» —
    # смешивать эти два «отсутствия» в одном счётчике значит потерять оба.
    measured = [f["age_s"] for f in plane_fields if isinstance(f.get("age_s"), (int, float))]
    return {
        "ts": row.get("ts") or datetime.now(UTC).isoformat(),
        "symbol": symbol,
        "tick_path": row.get("tick_path"),
        "tick_age_s": fresh.get("tick_age_s"),
        "dom_age_s": fresh.get("dom_age_s"),
        "fields": fields,
        "summary": {
            # `measured_plane_count` — главный числовой признак здоровья САМОГО аудита: если он
            # снова уедет в 0, значит источник возраста опять отвалился, как это было год до
            # 2026-07-26. Отдельным полем, чтобы деградацию было видно без чтения `fields`.
            "measured_plane_count": len(measured),
            "plane_count": len(plane_fields),
            "median_plane_age_s": (
                round(sorted(measured)[len(measured) // 2], 2) if measured else None
            ),
            "max_plane_age_s": round(max(measured), 2) if measured else None,
            "stale_plane_count": sum(1 for f in plane_fields if f.get("stale")),
            "absent_plane_count": sum(1 for f in plane_fields if f.get("source") == "absent"),
            # Скаляр, у которого ключа нет в market-словаре, — отдельный счётчик: это «значения
            # нет», а не «плана нет».
            "missing_value_count": sum(1 for f in value_fields if not f.get("present")),
        },
    }


def append_data_plane_audit(row: dict[str, Any]) -> None:
    if not data_plane_audit_enabled():
        return
    if row.get("error"):
        return
    try:
        from hunt_core.data.jsonl_io import append_jsonl_lines, rotate_jsonl_if_needed

        record = build_data_plane_audit(row)
        DATA_PLANE_AUDIT_JSONL.parent.mkdir(parents=True, exist_ok=True)
        # Ротация добавлена 2026-07-26: строка пишется на КАЖДЫЙ символ КАЖДОГО тика, и файл
        # спокойно дорос до 25 МБ — синхронный append в event loop по такому файлу дешевле не
        # становится. Остальные JSONL этого дерева ротируются тем же хелпером.
        rotate_jsonl_if_needed(DATA_PLANE_AUDIT_JSONL)
        append_jsonl_lines(
            DATA_PLANE_AUDIT_JSONL,
            [serde.dumps_str(record)],
        )
    except Exception:
        import structlog

        structlog.get_logger("hunt_core.diagnostics.data_plane_audit").debug(
            "data_plane_audit_write_failed", exc_info=True
        )


__all__ = [
    "append_data_plane_audit",
    "build_data_plane_audit",
    "data_plane_audit_enabled",
]
