"""Deep-delivery config — signal_queue/delivery cadence only.

Trimmed (2026-07): ``SignalGates``, ``TradePlanConfig`` (incl. the ``min_rr_tp1``
value that duplicated ``deep/pipeline/config.py::RiskConfig`` — that whole file is
also deleted now), ``priorities_a/b/c``, and the L0-L5-specific ambiguity/fragility/
disagreement/trade_rr thresholds were removed: they fed exclusively the deleted
``deep/engines/orchestrator.py`` (L0-L5 scenario engine). Nothing surviving
(signal_queue.py, delivery_policy.py, calibration.py, activation.py) ever read them —
confirmed by grep before deletion, not assumed. PrizrakTrade (``deep/prizrak/``) has
its own config (``deep/prizrak/config.py``) for its own thresholds.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class AnalystConfig:
    enabled: bool = True
    tg_verbose: bool = False
    signal_queue_enabled: bool = True
    signal_queue_top_n: int = 3
    signal_queue_tg_footer: bool = True
    signal_queue_tg_batch: bool = True
    signal_queue_tg_min_rank: int = 2
    signal_queue_ttl_hours: float = 2.5


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _defaults_path() -> Path:
    return Path(__file__).resolve().parents[3] / "config.defaults.toml"


def _load_toml_section() -> dict[str, Any]:
    path = _defaults_path()
    if not path.is_file():
        return {}
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    root = raw.get("analyst")
    return dict(root) if isinstance(root, dict) else {}


_KNOWN_ANALYST_ROOT_KEYS = frozenset(
    {
        "enabled",
        "tg_verbose",
        "signal_queue_enabled",
        "signal_queue_top_n",
        "signal_queue_tg_footer",
        "signal_queue_tg_batch",
        "signal_queue_tg_min_rank",
        "signal_queue_ttl_hours",
    }
)


def _reject_unknown_analyst_keys(section: dict[str, Any], *, allowed: frozenset[str]) -> None:
    unknown = sorted(k for k, v in section.items() if k not in allowed and not isinstance(v, dict))
    if unknown:
        raise ValueError(f"Unknown analyst config keys (fail-closed): {', '.join(unknown)}")


def load_analyst_config() -> AnalystConfig:
    root = _load_toml_section()
    _reject_unknown_analyst_keys(root, allowed=_KNOWN_ANALYST_ROOT_KEYS)

    verbose_env = os.getenv("HUNT_DEEP_TG_VERBOSE", "").strip().lower()
    verbose = verbose_env in {"1", "true", "yes"} if verbose_env else bool(root.get("tg_verbose", False))

    return AnalystConfig(
        enabled=bool(root.get("enabled", True)),
        tg_verbose=verbose,
        signal_queue_enabled=bool(root.get("signal_queue_enabled", True)),
        signal_queue_top_n=int(root.get("signal_queue_top_n", 3) or 3),
        signal_queue_tg_footer=bool(root.get("signal_queue_tg_footer", True)),
        signal_queue_tg_batch=_env_bool(
            "HUNT_SIGNAL_QUEUE_TG_BATCH", bool(root.get("signal_queue_tg_batch", True)),
        ),
        signal_queue_tg_min_rank=int(
            os.getenv("HUNT_SIGNAL_QUEUE_TG_MIN_RANK", root.get("signal_queue_tg_min_rank", 2)) or 2
        ),
        signal_queue_ttl_hours=float(root.get("signal_queue_ttl_hours", 2.5) or 2.5),
    )


__all__ = ["AnalystConfig", "load_analyst_config"]
