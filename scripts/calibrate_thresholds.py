#!/usr/bin/env python3
"""Data-driven threshold report for the scanner's hard gates — replaces guessing.

Every gate constant this session (`_INTRA_BAR_MICRO_ONLY_MIN`, `q_gate`, etc.) had
been set by eyeballing the code, not by looking at what the actual distribution of
values produced by the live pipeline looks like. That's how `_INTRA_BAR_MICRO_ONLY_MIN`
ended up above its own formula's mathematical ceiling (0.72 vs a 0.65 max) without
anyone noticing for who knows how long — a guess-then-guess-again constant with no
tether to reality.

This script does NOT change any threshold. It reports:
1. Whether the outcome ledger has enough resolved trades (close/sl_hit/timeout) to
   support a real win-rate-conditioned calibration at all — and refuses to fabricate
   one if the sample is too small (course-doctrine-consistent: abstain, don't guess).
2. The real quantile distribution of the metrics gates actually threshold on
   (fusion magnitude/score, pre_gate_energy), pulled straight from
   ``data/hunt_outcome_ledger.jsonl``.
3. Where a metric isn't persisted in the ledger at all (intra-bar micro-only
   `conf` — computed live, never written to a structured record), it says so
   explicitly instead of pretending to have data it doesn't.

Run: `.venv/bin/python scripts/calibrate_thresholds.py [--log PATH]`
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

from hunt_core.paths import ANALYST_TICKS_JSONL, OUTCOME_LEDGER

MIN_OUTCOMES_FOR_WIN_RATE_CALIBRATION = 100


def _load_ledger(path: Path) -> list[dict]:
    rows = []
    if not path.exists():
        return rows
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _quantiles(values: list[float], qs: tuple[float, ...] = (0.5, 0.7, 0.8, 0.9, 0.95)) -> dict[float, float]:
    if not values:
        return {}
    ordered = sorted(values)
    n = len(ordered)
    out = {}
    for q in qs:
        idx = min(n - 1, max(0, int(round(q * (n - 1)))))
        out[q] = ordered[idx]
    return out


def _print_quantiles(label: str, values: list[float]) -> None:
    if not values:
        print(f"  {label}: no data")
        return
    q = _quantiles(values)
    print(
        f"  {label}: n={len(values)} min={min(values):.3f} median={q[0.5]:.3f} "
        f"p70={q[0.7]:.3f} p80={q[0.8]:.3f} p90={q[0.9]:.3f} p95={q[0.95]:.3f} max={max(values):.3f}"
    )


def report_outcome_resolution(rows: list[dict]) -> None:
    events = Counter(r.get("event") for r in rows)
    resolved = events["close"] + events["sl_hit"] + events["timeout"]
    print("=== Outcome resolution (win/loss ground truth) ===")
    print(f"  candidate={events['candidate']} blocked={events['blocked']} delivered={events['delivered']}")
    print(f"  resolved outcomes (close+sl_hit+timeout): {resolved}")
    if resolved < MIN_OUTCOMES_FOR_WIN_RATE_CALIBRATION:
        print(
            f"  -> BELOW the {MIN_OUTCOMES_FOR_WIN_RATE_CALIBRATION}-outcome floor for a real "
            "win-rate-conditioned calibration. Refusing to fabricate a decile→p(win) "
            "table from this sample — that would be curve-fitting noise, exactly the "
            "'first verdict = leakage suspect, no edge yet' state already on record. "
            "Report quantile distributions only below; do not treat them as win-rate targets."
        )
    else:
        print("  -> enough resolved outcomes to attempt a real win-rate-conditioned calibration.")
    print()


def report_fusion_gate(rows: list[dict]) -> None:
    print("=== Fusion gate (scanner/detect/fusion.py) — fusion_score by blocker ===")
    by_reason: dict[str, list[float]] = {}
    for r in rows:
        if r.get("event") not in ("candidate", "blocked", "delivered"):
            continue
        score = r.get("fusion_score")
        if score is None:
            continue
        blockers = r.get("blockers") or ["(none)"]
        key = blockers[0] if blockers else "(none)"
        by_reason.setdefault(key, []).append(float(score))
    for key in sorted(by_reason, key=lambda k: -len(by_reason[k]))[:8]:
        _print_quantiles(key, by_reason[key])
    print()


def report_pre_gate_energy(rows: list[dict]) -> None:
    print("=== pre_gate_energy (scanner pre-phase energy) — by lifecycle_phase ===")
    by_phase: dict[str, list[float]] = {}
    for r in rows:
        if r.get("event") not in ("candidate", "blocked", "delivered"):
            continue
        e = r.get("pre_gate_energy")
        if e is None:
            continue
        phase = str(r.get("lifecycle_phase") or "(none)")
        by_phase.setdefault(phase, []).append(float(e))
    for phase in sorted(by_phase, key=lambda k: -len(by_phase[k])):
        _print_quantiles(phase, by_phase[phase])
    print()


_MICRO_SUPPRESSED_RE = re.compile(
    r"intra_bar_suppressed_low_conf sym=\S+ dir=\S+ conf=([0-9.]+) reason=(\S+) min=([0-9.]+)"
)
_MICRO_DELIVERED_RE = re.compile(r"intra_bar_delivered sym=\S+ dir=\S+ conf=([0-9.]+)")


def report_micro_only_conf_from_log(log_path: Path | None) -> None:
    print("=== intra-bar micro_only confidence — NOT in the ledger schema ===")
    print(
        "  `_real_intra_bar_confidence`'s (conf, reason) pair is computed live in "
        "deliver/intra_bar_delivery.py and only ever reaches a log line, never a "
        "persisted ledger field. That means there is no durable history to calibrate "
        "this specific threshold from — only whatever is in the current process's log "
        "file, which rotates/truncates on every restart. Recommend adding `conf`/"
        "`struct_reason` to the ledger's 'blocked' event payload so this stops being "
        "log-scraping and becomes a real queryable history."
    )
    if log_path is None or not log_path.exists():
        print(f"  (no log file given/found at {log_path} — skipping best-effort log parse)")
        print()
        return
    micro_conf: list[float] = []
    other_conf: list[float] = []
    delivered_conf: list[float] = []
    with log_path.open(errors="ignore") as f:
        for line in f:
            m = _MICRO_SUPPRESSED_RE.search(line)
            if m:
                conf, reason, _min = float(m.group(1)), m.group(2), float(m.group(3))
                (micro_conf if reason == "micro_only" else other_conf).append(conf)
                continue
            m2 = _MICRO_DELIVERED_RE.search(line)
            if m2:
                delivered_conf.append(float(m2.group(1)))
    print(f"  best-effort parse of {log_path.name} (this run only, not durable history):")
    print(
        "  CAVEAT: the 'suppressed' sample is left-censored by construction — anything "
        "that cleared the current threshold never logs as 'suppressed', so its max is "
        "mechanically capped at the current threshold value and cannot show what the "
        "distribution looks like ABOVE it. The 'delivered' sample below is what actually "
        "got through, which is what a real threshold discussion needs both sides of."
    )
    _print_quantiles("micro_only conf (suppressed, censored)", micro_conf)
    if other_conf:
        _print_quantiles("other-reason conf (suppressed)", other_conf)
    _print_quantiles("conf of signals actually delivered", delivered_conf)
    if micro_conf:
        q80 = _quantiles(micro_conf)[0.8]
        print(
            f"  If the intent is 'let through roughly the top 20% of raw micro-only "
            f"evidence, reject the rest' (a quantile policy, the same self-calibrating "
            f"idea the fusion gate already uses — not a flat guess), the suppressed-sample "
            f"p80 is {q80:.3f} — but per the caveat above this is a floor estimate, not the "
            f"true p80 of the full population. Confirm against a longer window AND the "
            f"delivered-signal sample before changing the constant — one run is not enough."
        )
    print()


def report_liquidation_synthetic_ratio(ticks_path: Path, *, tail: int = 2000) -> None:
    """How often the liquidation heatmap has ANY real event vs falling back to
    synthetic leverage-tier bands (``maps/liquidation.py``). Documented earlier
    this session as "~95% synthetic" from a static code read — this pulls the
    actual current ratio from ``analyst_ticks.jsonl`` instead of repeating that
    number from memory, so progress (or regression) after future changes to
    Bitget/other-venue liquidation coverage is measurable, not assumed.
    """
    print("=== Liquidation heatmap: real events vs synthetic fallback ===")
    if not ticks_path.exists():
        print(f"  (no tick file at {ticks_path})")
        print()
        return
    synth = 0
    real = 0
    lines = ticks_path.read_text(encoding="utf-8", errors="ignore").splitlines()[-tail:]
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        market = d.get("market") if isinstance(d, dict) else None
        if not isinstance(market, dict) or "liq_synthetic_only" not in market:
            continue
        if market.get("liq_synthetic_only"):
            synth += 1
        else:
            real += 1
    total = synth + real
    if total == 0:
        print(f"  (no rows with liq_synthetic_only in the last {tail} lines of {ticks_path.name})")
    else:
        print(
            f"  n={total} synthetic={synth} ({100 * synth / total:.1f}%) "
            f"real={real} ({100 * real / total:.1f}%)"
        )
        print(
            "  Still the dominant mode — real-event coverage (Binance/Bybit/OKX WS, "
            "Bitget self-gated pending CCXT support) has not meaningfully displaced "
            "synthetic leverage-tier bands. Not a quick-patch item: liquidation.py's own "
            "prior-session notes explicitly deferred sweep-decay/per-venue de-blurring "
            "to avoid unvalidated regressions in cascade math — track this ratio over "
            "time as the signal for whether that follow-up is worth doing."
        )
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--log", type=Path, default=Path("/tmp/hunt_watch.log"))
    ap.add_argument("--ledger", type=Path, default=OUTCOME_LEDGER)
    ap.add_argument("--ticks", type=Path, default=ANALYST_TICKS_JSONL)
    args = ap.parse_args()

    rows = _load_ledger(args.ledger)
    print(f"Loaded {len(rows)} rows from {args.ledger}\n")
    if not rows:
        print("No ledger data — nothing to calibrate.")
        return

    report_outcome_resolution(rows)
    report_fusion_gate(rows)
    report_pre_gate_energy(rows)
    report_micro_only_conf_from_log(args.log)
    report_liquidation_synthetic_ratio(args.ticks)


if __name__ == "__main__":
    main()
