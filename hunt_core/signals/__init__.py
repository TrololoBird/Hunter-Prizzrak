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

Nothing imports this PACKAGE — every consumer imports the submodules (`.lifecycle`,
`.model`, `.price_sanity`) directly — so the facade below is unreachable too. `__init__` is
still executed (importing `signals.lifecycle` runs the parent first), which is why vulture
never flagged it: `__all__` and the lazy import are real references to a dead surface. That
is what hid `emit_lifecycle_message` here until 2026-07-17, when it was deleted.

See CLAUDE.md § «Два модуля — НЕ ПУТАТЬ».
"""
from typing import Any

from hunt_core.signals.lifecycle import SignalLifecycleStore, compute_setup_id, process_lifecycle_tick
from hunt_core.signals.model import Signal, SignalModule, SignalState


def SignalEmitter(store: SignalLifecycleStore | None = None) -> Any:
    """Lazily construct `runtime.emitter.SignalEmitter` (breaks an import cycle).

    Signature mirrors the real class. It used to be `(*args: object, **kwargs: object)`
    behind an untyped `_lazy_emitter() -> tuple`, which defeated type-checking entirely —
    once the indirection went, mypy immediately caught that `object` was not
    `SignalLifecycleStore | None`. The facade had been lying about its own arguments for as
    long as nothing called it.

    NB it still has no callers — nothing imports the `hunt_core.signals` PACKAGE; every
    consumer imports the submodules directly. Kept because dropping the package's public
    surface is a wider call than the dead-code sweep that removed `emit_lifecycle_message`
    from beside it. If you are here to tidy: this and `__all__` go too, on the same evidence.
    """
    from hunt_core.runtime.emitter import SignalEmitter as _SignalEmitter  # noqa: PLC0415
    return _SignalEmitter(store)


__all__ = [
    "Signal",
    "SignalEmitter",
    "SignalLifecycleStore",
    "SignalModule",
    "SignalState",
    "compute_setup_id",
    "process_lifecycle_tick",
]
