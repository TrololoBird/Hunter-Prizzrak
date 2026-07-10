"""Shared signal lifecycle spine — Module 1 + Module 2 emit through here."""
from hunt_core.signals.lifecycle import SignalLifecycleStore, compute_setup_id, process_lifecycle_tick
from hunt_core.signals.model import Signal, SignalModule, SignalState


def _lazy_emitter() -> tuple:
    from hunt_core.runtime.emitter import SignalEmitter, emit_lifecycle_message  # noqa: PLC0415
    return SignalEmitter, emit_lifecycle_message


def SignalEmitter(*args: object, **kwargs: object) -> object:
    cls, _ = _lazy_emitter()
    return cls(*args, **kwargs)


def emit_lifecycle_message(*args: object, **kwargs: object) -> object:
    _, func = _lazy_emitter()
    return func(*args, **kwargs)


__all__ = [
    "Signal",
    "SignalEmitter",
    "SignalLifecycleStore",
    "SignalModule",
    "SignalState",
    "compute_setup_id",
    "emit_lifecycle_message",
    "process_lifecycle_tick",
]
