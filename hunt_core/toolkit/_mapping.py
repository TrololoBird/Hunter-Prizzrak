"""Typed access to nested ``dict`` fields on untyped rows.

Why this exists: the modules here read legacy untyped rows and repeatedly wrote::

    market = row.get("market") if isinstance(row.get("market"), dict) else {}

which is correct at runtime but opaque to the type checker — the ``isinstance`` guard
applies to the *second* ``.get`` call, not to the assigned expression, so the binding
stays ``Any | dict | None`` and every later ``market.get(...)`` is an error. That is why
``hunt_core.toolkit.*`` carried a blanket ``ignore_errors`` mypy override until
2026-07-26: the override hid 34 real narrowing complaints and, with them, any genuine
defect this package might have grown.

:func:`dict_field` says the same thing in one lookup and narrows properly, so the
override could be dropped instead of renewed.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["dict_field"]


def dict_field(source: Mapping[str, Any] | None, key: str) -> dict[str, Any]:
    """Return ``source[key]`` when it is a ``dict``, else an empty ``dict``.

    Semantics are identical to the ``x.get(k) if isinstance(x.get(k), dict) else {}``
    idiom it replaces — a missing key, a ``None`` value and a non-dict value all yield
    ``{}`` — with one lookup instead of two.

    Args:
        source: The mapping to read, or ``None``.
        key: Field name to read.

    Returns:
        The nested mapping, or an empty ``dict`` when absent or not a mapping.
    """
    if source is None:
        return {}
    value = source.get(key)
    return value if isinstance(value, dict) else {}
