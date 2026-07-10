"""Prizrak row serialization for JSONL / cache."""
from __future__ import annotations

from typing import Any


def strip_prizrak_for_jsonl(row: dict[str, Any]) -> dict[str, Any]:
    """Remove live dataclass before JSON encode."""
    out = dict(row)
    # Persist Scenario as serializable dict
    scenario = out.pop("scenario", None)
    if scenario is not None:
        to_dict = getattr(scenario, "to_dict", None)
        if callable(to_dict):
            out["scenario_summary"] = to_dict()
    return out
