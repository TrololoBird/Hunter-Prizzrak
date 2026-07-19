"""JSON I/O on ``orjson`` — the one library-native serde seam (replaces stdlib ``json``).

``orjson`` is the project's declared JSON library (the first entry in ``pyproject`` dependencies)
but was previously imported nowhere: every module hand-rolled stdlib ``json`` instead — a crutch
duplicating a faster, already-shipped library. This module routes all JSON I/O through ``orjson``
with the handful of options the codebase actually uses, while preserving stdlib semantics exactly:

* ``default=str`` fallback for types ``orjson`` cannot serialize natively (``Decimal``, arbitrary
  objects) — identical to the old ``json.dumps(..., default=str)`` sites;
* ``OPT_PASSTHROUGH_DATETIME`` so ``datetime`` is stringified via that same ``default`` (``str(dt)``,
  space-separated) instead of ``orjson``'s native RFC-3339 — byte-for-byte compatible with the
  existing JSONL ledgers so no ledger reader sees a format change;
* UTF-8 with non-ASCII preserved — ``orjson``'s default output equals stdlib ``ensure_ascii=False``.

``orjson.dumps`` returns ``bytes``; :func:`dumps_str` decodes to ``str`` for text-mode file handles,
which is what nearly every call site needs. :func:`dumps` exposes the raw ``bytes`` for hashing or
binary writes. :data:`JSONDecodeError` is re-exported so ``except`` clauses need not import ``orjson``
(it subclasses ``json.JSONDecodeError`` and ``ValueError``, so pre-existing catches keep working).
"""
from __future__ import annotations

from typing import Any

import orjson

# Re-export so call sites catch parse errors without importing orjson (⊆ json.JSONDecodeError).
JSONDecodeError = orjson.JSONDecodeError

_BASE_OPT = orjson.OPT_PASSTHROUGH_DATETIME


def _default(obj: Any) -> str:
    """Fallback for types ``orjson`` cannot serialize — mirrors the old ``default=str``."""
    return str(obj)


def dumps(obj: Any, *, indent: bool = False, sort_keys: bool = False) -> bytes:
    """Serialize ``obj`` to compact UTF-8 JSON **bytes**, ``str`` fallback for unknown types.

    Args:
        obj: The value to serialize (dict/list/scalar; ``datetime`` → ``str(dt)``).
        indent: Pretty-print with 2-space indentation (``orjson`` supports only 2).
        sort_keys: Emit object keys in sorted order (stable output).

    Returns:
        The JSON encoding as ``bytes``.
    """
    opt = _BASE_OPT
    if indent:
        opt |= orjson.OPT_INDENT_2
    if sort_keys:
        opt |= orjson.OPT_SORT_KEYS
    return orjson.dumps(obj, default=_default, option=opt)


def dumps_str(obj: Any, *, indent: bool = False, sort_keys: bool = False) -> str:
    """Like :func:`dumps` but decoded to ``str`` for text-mode writes.

    Args:
        obj: The value to serialize.
        indent: Pretty-print with 2-space indentation.
        sort_keys: Emit object keys in sorted order.

    Returns:
        The JSON encoding as a ``str``.
    """
    return dumps(obj, indent=indent, sort_keys=sort_keys).decode("utf-8")


def loads(data: str | bytes | bytearray) -> Any:
    """Parse JSON from ``str``/``bytes`` — a drop-in for ``json.loads``.

    Args:
        data: The JSON document.

    Returns:
        The decoded Python object.

    Raises:
        JSONDecodeError: If ``data`` is not valid JSON.
    """
    return orjson.loads(data)


__all__ = ["JSONDecodeError", "dumps", "dumps_str", "loads"]
