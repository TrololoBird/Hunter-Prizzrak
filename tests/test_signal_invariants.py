"""High-level engine invariants — properties that must survive a full code regen.

These are deliberately NOT unit tests of internal helpers. They pin three contracts of
the *production* manipulation detector (``advance_manipulation_scales``) replayed over the
in-repo historical parquet (``research/dataset_v8``), so they keep meaning even when the
internals are rewritten:

1. determinism   — identical input → byte-identical setup decisions.
2. no lookahead  — a decision made at time T never changes when *future* bars are added;
                   truncating the dataset past T leaves every decision at ≤ T untouched.
3. sanity        — the engine actually produces setups, and none is degenerate
                   (finite, positive entry/stop, entry ≠ stop, no NaN) — the Step-1
                   "сигналы вообще вменяемы" property.

The public-API invariant ("no private CCXT call") is enforced mechanically by ruff +
scripts/check_prohibited_apis.py, so it is not duplicated here.
"""
from __future__ import annotations

import glob
import math
import os

import pytest

from hunt_core.scanner.detect.patterns import advance_manipulation_scales

_HERE = os.path.dirname(os.path.abspath(__file__))
_DS = os.path.join(os.path.dirname(_HERE), "research", "dataset_v8")

TF_MS = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000,
         "1d": 86_400_000, "1w": 604_800_000}
USE_TF = ["1w", "1d", "4h", "1h", "15m", "5m"]

pytestmark = pytest.mark.skipif(
    not glob.glob(os.path.join(_DS, "*_1h.parquet")),
    reason="research/dataset_v8 parquet not present",
)


def _symbols() -> list[str]:
    return sorted({os.path.basename(f).rsplit("_", 1)[0] for f in glob.glob(f"{_DS}/*.parquet")})


def _load(sym: str) -> dict[str, list[list[float]]]:
    import polars as pl

    out: dict[str, list[list[float]]] = {}
    for tf in USE_TF:
        f = os.path.join(_DS, f"{sym}_{tf}.parquet")
        if not os.path.exists(f):
            continue
        df = pl.read_parquet(f)
        ts_col = "timestamp" if "timestamp" in df.columns else "time"
        df = df.sort(ts_col)
        ts = df[ts_col]
        if ts.dtype == pl.Datetime:
            ts = ts.dt.epoch(time_unit="ms")
        out[tf] = [
            [int(t), float(o), float(h), float(l), float(c), float(v)]
            for t, o, h, l, c, v in zip(ts, df["open"], df["high"], df["low"], df["close"], df["volume"])
        ]
    return out


def _closed_upto(rows: list[list[float]], tf: str, t: int) -> list[list[float]]:
    dur = TF_MS[tf]
    return [r for r in rows if r[0] + dur <= t]


def _fingerprint(setup) -> tuple:
    """Stable identity of a setup decision, from public fields only."""
    return (
        setup.direction,
        setup.pattern_type,
        setup.macro_tf,
        setup.meso_tf,
        round(float(setup.score), 6),
        None if setup.entry_ref is None else round(float(setup.entry_ref), 10),
        None if setup.target is None else round(float(setup.target), 10),
    )


def _replay(data: dict[str, list[list[float]]], *, cutoff: int | None = None) -> dict[int, tuple]:
    """Replay the production detector bar-by-bar; return {scan_time: setup_fingerprint}.

    Only bars whose CLOSE time is ≤ scan-time T feed the detector (no lookahead), exactly
    like the live scan cadence. ``cutoff`` drops every raw bar with open-time ≥ cutoff
    before replaying — used to prove that removing future data changes nothing at ≤ T.
    """
    if cutoff is not None:
        data = {tf: [r for r in rows if r[0] < cutoff] for tf, rows in data.items()}
    decisions: dict[int, tuple] = {}
    states = None
    for r in data.get("1h", []):
        t = r[0] + TF_MS["1h"]
        oc = {tf: _closed_upto(rows, tf, t) for tf, rows in data.items()}
        oc = {tf: v for tf, v in oc.items() if len(v) >= 20}
        if "4h" not in oc and "1d" not in oc:
            continue
        states, setup = advance_manipulation_scales("SYM", oc, states, now_ms=t)
        if setup is not None:
            decisions[t] = _fingerprint(setup)
    return decisions


def _first_symbol_with_setups() -> tuple[str, dict[str, list[list[float]]], dict[int, tuple]]:
    for sym in _symbols():
        data = _load(sym)
        if "1h" not in data:
            continue
        dec = _replay(data)
        if dec:
            return sym, data, dec
    pytest.skip("no symbol in dataset_v8 produced a setup")


def test_engine_produces_sane_setups() -> None:
    """Invariant 3: the engine emits ≥1 setup and none is degenerate."""
    total = 0
    for sym in _symbols():
        data = _load(sym)
        if "1h" not in data:
            continue
        states = None
        for r in data["1h"]:
            t = r[0] + TF_MS["1h"]
            oc = {tf: _closed_upto(rows, tf, t) for tf, rows in data.items()}
            oc = {tf: v for tf, v in oc.items() if len(v) >= 20}
            if "4h" not in oc and "1d" not in oc:
                continue
            states, setup = advance_manipulation_scales(sym, oc, states, now_ms=t)
            if setup is None:
                continue
            total += 1
            entry = setup.entry_ref
            assert entry is not None and math.isfinite(entry) and entry > 0, (sym, entry)
            assert math.isfinite(setup.score), (sym, setup.score)
            if setup.target is not None:
                assert math.isfinite(setup.target) and setup.target > 0, (sym, setup.target)
                assert setup.target != entry, (sym, entry)
    assert total >= 1, "manipulation engine produced 0 setups on dataset_v8 — red flag"


def test_detector_is_deterministic() -> None:
    """Invariant 1: identical inputs → identical decisions across two full replays."""
    sym, data, first = _first_symbol_with_setups()
    second = _replay(data)
    assert first == second, f"non-deterministic decisions for {sym}"


def test_no_lookahead_bias() -> None:
    """Invariant 2: dropping bars after a cutoff leaves every decision at ≤ cutoff intact."""
    sym, data, full = _first_symbol_with_setups()
    bars = data["1h"]
    # Cut ~30% of the history off the end; there must still be earlier decisions to compare.
    cut_idx = int(len(bars) * 0.7)
    cutoff = bars[cut_idx][0]
    truncated = _replay(data, cutoff=cutoff)
    overlap = {t: fp for t, fp in full.items() if t <= cutoff}
    assert overlap, f"no decisions before cutoff for {sym} — pick a later cutoff"
    for t, fp in overlap.items():
        assert truncated.get(t) == fp, (
            f"lookahead: decision at t={t} changed when future bars were removed ({sym})"
        )
