"""Central tick diagnostics — one hook at JSONL persistence boundary."""
from __future__ import annotations

from typing import Any


def append_tick_diagnostics(row: dict[str, Any]) -> None:
    """Append universe + data-plane audit rows for a persisted tick."""
    if not isinstance(row, dict):
        return
    # `liquidity_skip` убран из гарда 2026-07-26: продюсер ключа ушёл с легаси-транспортом
    # (`5ba0fea`), а строку сегодня собирают только `_serialize_native_scan_row` и `_error_row` —
    # ни один из них его выставить не может. Терм молча не срабатывал.
    if row.get("error"):
        return
    try:
        from hunt_core.diagnostics.data_plane_audit import append_data_plane_audit
        from hunt_core.diagnostics.universe_audit import append_tick_universe_audit

        append_data_plane_audit(row)
        append_tick_universe_audit(row)
    except Exception:
        import structlog

        structlog.get_logger("hunt_core.diagnostics.tick_diagnostics").debug(
            "tick_diagnostics_failed", symbol=row.get("symbol"), exc_info=True
        )


__all__ = ["append_tick_diagnostics"]
