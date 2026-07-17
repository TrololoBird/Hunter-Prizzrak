"""PRIZRAK-only signal lifecycle — despite the `SignalModule = Literal[1, 2]` shape.

The docstring here used to read «Shared signal lifecycle spine — Module 1 + Module 2 emit
through here». That was a name-lie, and an expensive one: it asserts the two strategies are
unified where they are not, so anyone reading it believes манипуляции flow through here too.
They do not.

What is actually true:
* `lifecycle.process_lifecycle_tick` reads `row["prizrak_summary"]` and returns
  `event="none", suppress_reason="wait_or_no_setup"` when it is absent — so a scanner row
  passed with `module=2` would be silently SUPPRESSED, not emitted;
* its only callers are `runtime/emitter.py:18,31`, both hardcoded `module=1`;
* манипуляции emit on a completely separate path — `deliver/manipulation_delivery.py::
  deliver_manipulation_setups` detects AND delivers in one call, on its own 300s timer.

The two lanes converge only AFTER emission, at `track/tracker.py::register_signal_open`
(shared `paths.SIGNAL_STATE`) and at the shared `TelegramBroadcaster`. There is no shared
row dict and no shared spine. `module=2` is unreachable scaffolding; keep it or delete it,
but do not read it as evidence that this module is generic.

See CLAUDE.md § «Два модуля — НЕ ПУТАТЬ».
"""
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
