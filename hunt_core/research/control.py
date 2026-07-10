"""Control-cohort generator — the null model the whole loop hinges on.

For every real signal we synthesize matched controls scored through the *same*
metric engine and the *same* forward path. If the real cohort's winrate/return
is indistinguishable from a control, the apparent "edge" is regime or lookahead
leakage, not signal quality.

Control kinds:
- ``coin_flip``   — identical geometry/time; direction chosen by a seeded coin.
- ``random_time`` — same symbol/direction; entry shifted to a random bar inside
  the early part of the fetched window (entry = that bar's open), forward path
  = bars from there on. Tests "was the *timing* special, or just the symbol?".
- ``naive_long`` / ``naive_short`` — fixed direction, same trigger. Tests "does
  a dumb always-long/always-short do as well?".

Reproducibility: the RNG is seeded from a global seed + the signal_id, so a
control is a pure function of the real signal (order-independent, re-runnable).
"""
from __future__ import annotations

import hashlib
import random
from typing import Any

# Fraction of the forward window in which a random_time control may re-enter.
# Kept in the early part so a meaningful path still remains after the shift.
_RANDOM_TIME_MAX_FRAC = 0.25

CONTROL_KINDS = ("coin_flip", "random_time", "naive_long", "naive_short")


def _seeded_rng(seed: int, signal_id: str) -> random.Random:
    h = hashlib.sha256(f"{seed}:{signal_id}".encode()).hexdigest()
    return random.Random(int(h[:16], 16))


def make_controls(
    *,
    signal_id: str,
    direction: str,
    entry: float,
    t0_ms: int,
    forward_ohlcv: list[list[float]],
    seed: int,
    kinds: tuple[str, ...] = CONTROL_KINDS,
) -> list[dict[str, Any]]:
    """Return control specs for one real signal.

    Each spec: ``{control_kind, direction, entry, t0_ms, forward_ohlcv}`` — ready
    to feed straight into ``outcome_store.build_outcome_row``.
    """
    rng = _seeded_rng(seed, signal_id)
    out: list[dict[str, Any]] = []
    for kind in kinds:
        if kind == "coin_flip":
            d = "long" if rng.random() < 0.5 else "short"
            out.append({
                "control_kind": kind, "direction": d, "entry": entry,
                "t0_ms": t0_ms, "forward_ohlcv": forward_ohlcv,
            })
        elif kind == "naive_long":
            out.append({
                "control_kind": kind, "direction": "long", "entry": entry,
                "t0_ms": t0_ms, "forward_ohlcv": forward_ohlcv,
            })
        elif kind == "naive_short":
            out.append({
                "control_kind": kind, "direction": "short", "entry": entry,
                "t0_ms": t0_ms, "forward_ohlcv": forward_ohlcv,
            })
        elif kind == "random_time":
            n = len(forward_ohlcv)
            if n < 4:
                continue  # not enough path to shift and still measure
            hi = max(1, int(n * _RANDOM_TIME_MAX_FRAC))
            j = rng.randint(1, hi)
            bar = forward_ohlcv[j]
            new_t0 = int(bar[0])
            new_entry = float(bar[1])  # open of the re-entry bar
            if new_entry <= 0:
                continue
            out.append({
                "control_kind": kind, "direction": direction, "entry": new_entry,
                "t0_ms": new_t0, "forward_ohlcv": forward_ohlcv[j:],
            })
    return out


__all__ = ["CONTROL_KINDS", "make_controls"]
