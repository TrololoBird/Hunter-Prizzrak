"""Минимальный номинальный пол дистанции стопа — единственное, что от `levels/` осталось живым.

История файла (нужна, чтобы его не «восстановили»). До 2026-07-26 здесь лежало 1575 строк
геометрии SL/TP: два построителя уровней (`structural_long_levels` / `structural_short_levels`),
две TP-лестницы по ликвидности, `adaptive_level_params`, `build_liquidity_context`,
`continuation_short_targets`, `reanchor_setup_levels`, `fib_retracement_levels`. **Ни у одной
из них не было вызова из прода** — они приехали целиком в стартовом снимке репозитория
(`b67d659`, 2026-07-09) и ни разу не были подключены; геометрию сетапов в этом проекте считает
`hunt_core/prizrak/` (`setups.py`, `grid.py`), а полосу манипуляций — `scanner/detect/patterns.py`.

Почему это не заметили полторы недели:

* покрытие 17% читалось как «недотестировано», а не как «не исполняется»;
* vulture с `min_confidence = 80` публичную функцию, которую импортирует хотя бы один модуль,
  уверенной находкой не считает — а `features/fib.py` импортировал `fib_retracement_levels`
  и при этом сам не был импортирован никем;
* `CLAUDE.md` описывал каталог как «чистая геометрия SL/TP+fib», то есть как действующую
  ответственность.

Вскрылось при попытке закрыть модуль тестами: независимый пересчёт R:R разошёлся с полем
модуля (2.72 против 1.97), и корнем оказалась инверсия — `worst` анкерил ЛУЧШИЙ залив вопреки
имени и комментарию (`long → entry_lo`, `short → entry_hi`), завышая R:R и ослабляя вето
`sl_nominal_too_wide`. Канон `hunt_core/contract.py::worst_entry_edge` описывает ровно эту
инверсию как однажды уже исправленную — в мёртвой копии она пережила ту правку. Ущерба не
нанесла именно потому, что код не исполнялся. Удалено вместе с ним; история в git.

Достижимость закреплена `tests/test_levels_reachability.py`.
"""

from __future__ import annotations

# Общий пол: стоп не ближе 1% номинала. Якорные мажоры ходят спокойнее, поэтому им позволен
# вдвое с половиной более узкий стоп — иначе номинальный пол, а не структура, диктовал бы риск.
SHORT_MIN_SL_DIST_PCT = 1.0
LONG_MIN_SL_DIST_PCT = 1.0
_ANCHOR_SYMBOLS = frozenset({"BTCUSDT", "ETHUSDT", "XAUUSDT", "XAGUSDT"})
_ANCHOR_SHORT_MIN_SL_DIST_PCT = 0.40
_ANCHOR_LONG_MIN_SL_DIST_PCT = 0.40

__all__ = [
    "LONG_MIN_SL_DIST_PCT",
    "SHORT_MIN_SL_DIST_PCT",
    "long_min_sl_dist_pct",
    "short_min_sl_dist_pct",
]


def _anchor_key(symbol: str) -> str:
    """Привести любое написание символа к форме ``BTCUSDT``, которой ключуется ``_ANCHOR_SYMBOLS``.

    Суффикс расчёта снимается ДО разделителей: канонический unified-формат проекта —
    ``BTC/USDT:USDT``, и удаление только ``-`` и ``/`` оставляло ``BTCUSDT:USDT``, который не
    совпадал ни с одним якорем, — вызывающий с unified-символом молча получал общий пол 1.0
    вместо 0.4. Живого ущерба не было (единственный вызывающий,
    ``confluence/mtf.py::build_mtf_confluence_native``, нормализует сам), но корректность
    зависела от того, повторит ли этот шаг каждый следующий. Исправлено 2026-07-26.

    Args:
        symbol: Символ в любом написании — ``BTCUSDT``, ``BTC/USDT:USDT``, ``btc-usdt``.

    Returns:
        Ключ поиска по якорям в верхнем регистре без разделителей и суффикса расчёта.
    """
    return str(symbol or "").split(":", 1)[0].upper().replace("-", "").replace("/", "")


def short_min_sl_dist_pct(symbol: str) -> float:
    """Минимальная номинальная дистанция стопа (%) для шорта.

    Args:
        symbol: Символ инструмента в любом написании.

    Returns:
        Пол дистанции в процентах: якорный для мажоров, общий для остальных.
    """
    if _anchor_key(symbol) in _ANCHOR_SYMBOLS:
        return _ANCHOR_SHORT_MIN_SL_DIST_PCT
    return SHORT_MIN_SL_DIST_PCT


def long_min_sl_dist_pct(symbol: str) -> float:
    """Минимальная номинальная дистанция стопа (%) для лонга.

    Args:
        symbol: Символ инструмента в любом написании.

    Returns:
        Пол дистанции в процентах: якорный для мажоров, общий для остальных.
    """
    if _anchor_key(symbol) in _ANCHOR_SYMBOLS:
        return _ANCHOR_LONG_MIN_SL_DIST_PCT
    return LONG_MIN_SL_DIST_PCT
