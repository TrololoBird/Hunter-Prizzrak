"""Tradable-symbol gate — single filter for universe, WS, ignition, scanner."""
from __future__ import annotations

from typing import Any

from hunt_core.market.symbols import filter_tradable_symbols, is_tradable_linear_usdt


def gate_symbol_list(
    symbols: list[str],
    *,
    exchange: Any,
    label: str = "universe",
) -> list[str]:
    return filter_tradable_symbols(symbols, exchange=exchange, label=label)


def gate_symbol_dict_keys(
    mapping: dict[str, Any],
    *,
    exchange: Any,
    label: str = "mapping",
) -> dict[str, Any]:
    allowed = set(filter_tradable_symbols(list(mapping.keys()), exchange=exchange, label=label))
    return {k: v for k, v in mapping.items() if k in allowed}


def is_allowed_for_analysis(symbol: str, *, exchange: Any) -> bool:
    return is_tradable_linear_usdt(symbol, exchange=exchange)


__all__ = [
    "gate_symbol_dict_keys",
    "gate_symbol_list",
    "is_allowed_for_analysis",
]
