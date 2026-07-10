"""Hunter async runtime — lazy exports to avoid import cycles."""

from __future__ import annotations

from typing import Any

__all__ = ["run_loop", "run_tick", "request_stop"]


def __getattr__(name: str) -> Any:
    if name in {"run_loop", "run_tick"}:
        from hunt_core.runtime.cycle import run_loop, run_tick

        return run_loop if name == "run_loop" else run_tick
    if name == "request_stop":
        from hunt_core.runtime.state import request_stop

        return request_stop
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
