#!/usr/bin/env python3
"""Monitor a live `hunt_core watch` process and classify its log stream.

Operator/CI harness for the WS-1 stability gate: tails the bot's log file, classifies every
line into errors / bans-ratelimit / reconnects / stale-empty / watchdog, tracks whether the
process is alive, and **exits early the moment the bot dies** (so a death is observed, not
inferred). Prints a running snapshot each sample and a final summary. Read-only; touches no
project code — safe to run alongside a live watch.

Usage:
    # launch the bot writing to a log, then:
    uv run python -m scripts.monitor_live --log hunt/live.log --duration-min 120
    uv run python -m scripts.monitor_live --log hunt/live.log --duration-min 120 --pattern "hunt_core watch"
"""
from __future__ import annotations

import argparse
import re
import subprocess
import time

_CATS: dict[str, re.Pattern[str]] = {
    "errors": re.compile(r"traceback|exception|critical|fatal|\berror\b(?!_rate)", re.I),
    "bans_rl": re.compile(r"\b418\b|\b429\b|rate.?limit|too many|banned|ip.?ban|retcode.*1000[36]", re.I),
    "reconnects": re.compile(r"reconnect|disconnect|ws.*(closed|restart)|stream.*(restart|down)", re.I),
    "stale_empty": re.compile(r"blackout|stale|\bempty\b|no data|no_data|universe_health|degraded", re.I),
    "watchdog": re.compile(r"watchdog.*(fired|trip|kill|dump)|dump_traceback", re.I),
}
_BENIGN = re.compile(r"error_rate|otel_disabled|no error|0 error|watchdog_armed", re.I)
_HEALTHY = re.compile(
    r"hunt_scan_done|prizrak_enrich_done|prepared symbol successfully|telegram message sent", re.I
)


def _alive(pattern: str) -> bool:
    return subprocess.run(["pgrep", "-f", pattern], capture_output=True).returncode == 0


def _classify(lines: list[str], totals: dict[str, int], hits: dict[str, list[str]]) -> int:
    healthy = 0
    for ln in lines:
        if _BENIGN.search(ln):
            continue
        if _HEALTHY.search(ln):
            healthy += 1
        for k, rx in _CATS.items():
            if rx.search(ln):
                totals[k] += 1
                if len(hits[k]) < 10:
                    hits[k].append(ln.strip()[:200])
    return healthy


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", required=True, help="path to the bot's log file")
    ap.add_argument("--duration-min", type=float, default=120.0)
    ap.add_argument("--sample-s", type=float, default=30.0)
    ap.add_argument("--pattern", default="hunt_core watch", help="pgrep pattern for the bot process")
    args = ap.parse_args()

    start = time.monotonic()
    offset = 0
    totals = {k: 0 for k in _CATS}
    hits: dict[str, list[str]] = {k: [] for k in _CATS}
    total_healthy = 0
    death_at: float | None = None

    while time.monotonic() - start < args.duration_min * 60:
        try:
            with open(args.log, errors="replace") as f:
                f.seek(offset)
                new = f.readlines()
                offset = f.tell()
        except FileNotFoundError:
            new = []
        total_healthy += _classify(new, totals, hits)
        el = int(time.monotonic() - start)
        print(f"[t+{el // 60}m{el % 60:02d}s] alive={_alive(args.pattern)} "
              f"healthy={total_healthy} " + " ".join(f"{k}={totals[k]}" for k in _CATS), flush=True)
        if not _alive(args.pattern):
            death_at = el
            break
        time.sleep(args.sample_s)

    el = int(time.monotonic() - start)
    print("\n===== MONITOR SUMMARY =====")
    if death_at is not None:
        print(f"*** BOT DIED at t+{death_at // 60}m{death_at % 60:02d}s ***")
    else:
        print(f"bot ran the full {el // 60}m without dying")
    print(f"healthy_events={total_healthy}")
    for k in _CATS:
        print(f"{k}={totals[k]}")
        for x in hits[k]:
            print(f"    | {x}")
    return 1 if death_at is not None else 0


if __name__ == "__main__":
    raise SystemExit(main())
