"""Universe audit — состояние вселенной на входе конвейера, строка на символ за тик.

⚠ Слово «prescan» ушло из имени модуля не для красоты. Воронка `prescan` жила в
`hunt_core/scanner/`, вырезанном 2026-07-31, а три колонки (`prescan_energy`,
`prescan_direction`, `prescan_change_pct`) остались читать ключ `prescan_outlier`, которого
с тех пор не пишет НИКТО. Замер 2026-08-02: во всех **2033 из 2033** строк
`data/universe_audit.jsonl` эти поля равны `null`. Это не «выбросов не было» — это прибор,
у которого отключён датчик, и по строке файла одно от другого не отличить (I-6).
Колонки сняты; вернуть их можно только вместе с продюсером.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import structlog

from hunt_core import serde
from hunt_core.paths import UNIVERSE_AUDIT_JSONL

LOG = structlog.get_logger(__name__)


def universe_audit_enabled() -> bool:
    return os.getenv("HUNT_UNIVERSE_AUDIT", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def append_tick_universe_audit(row: dict[str, Any]) -> None:
    """Log per-tick universe state after snapshot (phase + lifecycle at pipeline entry)."""
    if not universe_audit_enabled():
        return
    # `liquidity_skip` снят 2026-07-26 — сирота без продюсера с `5ba0fea` (см. tick_diagnostics).
    if row.get("error"):
        return
    try:
        from hunt_core.data.jsonl_io import append_jsonl_lines

        _lc = row.get("lifecycle")
        lc = _lc if isinstance(_lc, dict) else {}
        # `prescan_*` сняты 2026-08-02 вместе с чтением `row["prescan_outlier"]`: продюсера
        # нет с выреза сканера, 2033 строки из 2033 несли null (см. докстроку модуля).
        # NB (audit R2 chunk 7): leg_gain_pct / fall_from_high_pct were dropped — no
        # producer anywhere writes those keys into the lifecycle dict (always null).
        # fusion_score was dropped too: row["dump"]/row["long"] are permanently
        # neutral stubs (tick_assembly) with no fusion_score/long_score keys, so the
        # field was always 0 → null. Don't re-add without a real producer.
        record = {
            "ts": row.get("ts") or datetime.now(UTC).isoformat(),
            "event": "tick_snapshot",
            "symbol": str(row.get("symbol") or "").upper(),
            "tick_path": row.get("tick_path"),
            "snapshot_tier": row.get("snapshot_tier"),
            "chg_24h_pct": row.get("chg_24h_pct"),
            "phase": lc.get("phase") or lc.get("phase_fusion"),
            "watch_ok": lc.get("watch_ok"),
            "cusum": lc.get("cusum"),
            "cusum_band": lc.get("cusum_band") or lc.get("band"),
            "recommended_bias": lc.get("recommended_bias") or lc.get("bias"),
            "ignited": bool(row.get("ignited")),
        }
        UNIVERSE_AUDIT_JSONL.parent.mkdir(parents=True, exist_ok=True)
        append_jsonl_lines(
            UNIVERSE_AUDIT_JSONL,
            [serde.dumps_str(record)],
        )
    except (OSError, TypeError, ValueError) as exc:
        # ⚠ Здесь стоял `pass`. Аудит, который молча не пишется, неотличим от аудита, в
        # котором ничего не происходило, — а по нему потом меряют вселенную. Гейт `S110`
        # это НЕ ловил: замер 2026-08-02 показал, что ruff S110/S112 срабатывают только на
        # `except Exception`, а на типизированный кортеж — нет (проверено на пробнике из
        # четырёх форм: 1 находка из 4). Отказ теперь виден, работа тика не прерывается.
        LOG.warning(
            "universe_audit_append_failed",
            symbol=str(row.get("symbol") or ""),
            path=str(UNIVERSE_AUDIT_JSONL),
            error=repr(exc),
        )


__all__ = [
    "append_tick_universe_audit",
    "universe_audit_enabled",
]
