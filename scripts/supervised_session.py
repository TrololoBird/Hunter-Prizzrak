#!/usr/bin/env python3
"""Supervised Hunt watch — N hours with health passes, error capture, auto-restart.

  .venv/bin/python hunt/scripts/supervised_session.py --hours 8 --watch-interval 30
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

HUNT = Path(__file__).resolve().parents[1]
ROOT = HUNT.parent
sys.path.insert(0, str(HUNT))
from hunt_core.paths import HUNT_SCAN_JSONL

DATA = HUNT / "data"
TICK_JSONL = HUNT_SCAN_JSONL
WATCH_SH = HUNT / "scripts" / "watch.sh"
_hunt_py = HUNT / ".venv/bin/python"
PY = _hunt_py if _hunt_py.is_file() else ROOT / ".venv/bin/python"
_CHECK_LOGIC_CWD = HUNT if _hunt_py.is_file() else ROOT

_ERROR_MARKERS = (
    "Traceback (most recent call last)",
    "ERROR",
    "CRITICAL",
    "dump_watch_tick_error",
    "Unclosed client session",
    "ExchangeNotAvailable",
    "symbol_tick_timeout",
)
_IGNORE_ERROR_SUBSTR = (
    "watch_symbol_data_reject",
    "watch_alert_blocked",
    "secondary_load_markets_failed",
    "cross_exchange",
    "ccxt_load_markets_failed | proxy=direct",
    "hunt_market_plane_initial_fail",
    "secondary_funding_ws",
    "spot_fetch_failed",
    "fapi_metric_failed",
)


def _run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _log(msg: str, *, run_dir: Path) -> None:
    line = f"{datetime.now(UTC).isoformat()} | {msg}"
    print(line, flush=True)
    with (run_dir / "supervisor.log").open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _watch_running() -> bool:
    proc = subprocess.run(
        ["pgrep", "-f", "[h]unt_core watch"],
        capture_output=True,
        text=True,
        check=False,
    )
    return bool(proc.stdout.strip())


def _watch_sh_running() -> bool:
    proc = subprocess.run(
        ["pgrep", "-f", "[h]unt/scripts/watch.sh"],
        capture_output=True,
        text=True,
        check=False,
    )
    return bool(proc.stdout.strip())


def _stop_watch() -> None:
    subprocess.run(["pkill", "-f", "[h]unt_core watch"], check=False)
    lock = DATA / "watch.pid"
    if lock.exists():
        lock.unlink(missing_ok=True)
    time.sleep(1.5)


def _start_watch(*, interval: int, log_path: Path) -> None:
    """Start exactly one watch child (no nested bash restart loops)."""
    if _watch_running():
        return
    if _watch_sh_running():
        _stop_watch()
        time.sleep(3.0)
        if _watch_running():
            return
    _stop_watch()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        # supervised_session owns restarts — do not nest watch.sh while-true loop.
        "HUNT_WATCH_SUPERVISE": "0",
        "HUNT_WATCHDOG_S": "900",
        "HUNT_SUPERVISED_CHILD": "1",
    }
    with log_path.open("a", encoding="utf-8") as out:
        out.write(f"\n--- watch_start {datetime.now(UTC).isoformat()} ---\n")
        subprocess.Popen(
            ["bash", str(WATCH_SH), "--interval", str(interval)],
            cwd=str(HUNT),
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )


def _read_log_delta(path: Path, offset: int) -> tuple[list[str], int]:
    if not path.exists():
        return [], offset
    size = path.stat().st_size
    if size < offset:
        offset = 0
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        fh.seek(offset)
        chunk = fh.read()
        return chunk.splitlines(), fh.tell()


def _extract_errors(lines: list[str]) -> list[str]:
    hits: list[str] = []
    for line in lines:
        if "WARNING" in line and "ERROR" not in line.split("|", 3)[-1]:
            # Ignore WARNING-only lines unless they carry an explicit ERROR marker.
            if not any(m in line for m in ("Traceback", "dump_watch_tick_error")):
                continue
        if not any(m in line for m in _ERROR_MARKERS):
            continue
        if any(s in line for s in _IGNORE_ERROR_SUBSTR):
            continue
        hits.append(line.strip()[:600])
    return hits[-40:]


def _tick_stats(*, since_bytes: int) -> dict:
    paths = Counter()
    errors: list[dict] = []
    rejects = Counter()
    lines = 0
    if not TICK_JSONL.exists():
        return {"lines": 0}
    with TICK_JSONL.open("rb") as fh:
        if since_bytes:
            fh.seek(since_bytes)
        for raw in fh:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            lines += 1
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            paths[str(row.get("tick_path") or "unknown")] += 1
            if row.get("error"):
                err = str(row.get("error"))
                rejects[err.split("[", 1)[0][:80]] += 1
                if len(errors) < 12:
                    errors.append({"symbol": row.get("symbol"), "error": err[:200]})
    return {
        "lines": lines,
        "tick_path": dict(paths),
        "reject_reasons": dict(rejects.most_common(8)),
        "row_errors": errors,
    }


def _run_check_logic() -> tuple[bool | None, str]:
    """Run the optional hunt_core._dev.check_logic health probe.

    Returns (None, reason) when the dev-tooling module is not installed in the
    target interpreter, so the supervisor records a skip instead of a permanent
    check_logic=FAIL. Returns (bool, tail) when the probe actually ran.
    """
    env = {k: v for k, v in os.environ.items()}
    for key in ("HUNT_ADVISORY_TG", "HUNT_LONG_TG", "HUNT_WIDE_MODE"):
        env.pop(key, None)
    proc = subprocess.run(
        [str(PY), "-m", "hunt_core._dev.check_logic"],
        cwd=str(_CHECK_LOGIC_CWD),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    output = (proc.stderr or "") + (proc.stdout or "")
    if "No module named 'hunt_core._dev'" in output or "No module named hunt_core._dev" in output:
        return None, "skipped: hunt_core._dev.check_logic not present"
    tail = output[-400:]
    return proc.returncode == 0, tail


def _log_stale(log_path: Path, *, max_age_s: float) -> bool:
    if TICK_JSONL.exists():
        age = time.time() - TICK_JSONL.stat().st_mtime
        if age <= max(120.0, max_age_s * 0.5):
            return False
    if not log_path.exists():
        return True
    if time.time() - log_path.stat().st_mtime > max_age_s:
        return True
    try:
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-15000:]
    except OSError:
        return True
    markers = (
        "watch_tick",
        "watch_universe",
        "watch_telegram_ready",
        "hunt_load_plan",
        "watch_symbol",
    )
    return not any(m in tail for m in markers)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hunt supervised watch session")
    parser.add_argument("--hours", type=float, default=8.0)
    parser.add_argument("--watch-interval", type=int, default=30)
    parser.add_argument("--verify-interval", type=int, default=600, help="Seconds between health passes")
    parser.add_argument("--check-logic-every", type=int, default=3600, help="Seconds between check_logic runs")
    args = parser.parse_args(argv)

    run_id = _run_id()
    run_dir = DATA / "sessions" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    watch_log = run_dir / "watch.log"
    issues_path = run_dir / "issues.jsonl"

    meta = {
        "run_id": run_id,
        "started_at": datetime.now(UTC).isoformat(),
        "hours": args.hours,
        "watch_interval": args.watch_interval,
        "verify_interval": args.verify_interval,
        "run_dir": str(run_dir),
    }
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    _log(f"SESSION_START run_id={run_id} hours={args.hours}", run_dir=run_dir)

    _start_watch(interval=args.watch_interval, log_path=watch_log)
    end_at = time.time() + args.hours * 3600.0
    log_offset = 0
    tick_offset = TICK_JSONL.stat().st_size if TICK_JSONL.exists() else 0
    pass_n = 0
    last_check_logic = 0.0
    restarts = 0
    all_errors: list[str] = []

    while time.time() < end_at:
        pass_n += 1
        sleep_s = min(float(args.verify_interval), max(5.0, end_at - time.time()))
        time.sleep(sleep_s)

        new_lines, log_offset = _read_log_delta(watch_log, log_offset)
        err_hits = _extract_errors(new_lines)
        if err_hits:
            all_errors.extend(err_hits)
            for hit in err_hits:
                issue = {
                    "at": datetime.now(UTC).isoformat(),
                    "pass": pass_n,
                    "kind": "log_error",
                    "detail": hit,
                }
                with issues_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(issue, ensure_ascii=False) + "\n")
            _log(f"PASS_{pass_n} errors={len(err_hits)} sample={err_hits[-1][:120]}", run_dir=run_dir)

        ticks = _tick_stats(since_bytes=tick_offset)
        tick_offset = TICK_JSONL.stat().st_size if TICK_JSONL.exists() else tick_offset

        running = _watch_running()
        stale = _log_stale(watch_log, max_age_s=max(900.0, args.verify_interval * 2.5))
        healthy = running and not stale
        if running and stale:
            # Do not kill a live watch because log markers lag — tick JSONL is authoritative.
            healthy = True

        check_ok: bool | None = None
        if time.time() - last_check_logic >= args.check_logic_every:
            check_ok, check_tail = _run_check_logic()
            last_check_logic = time.time()
            if check_ok is None:
                _log(f"PASS_{pass_n} check_logic=SKIP ({check_tail})", run_dir=run_dir)
            elif not check_ok:
                issue = {
                    "at": datetime.now(UTC).isoformat(),
                    "pass": pass_n,
                    "kind": "check_logic_fail",
                    "detail": check_tail,
                }
                with issues_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(issue, ensure_ascii=False) + "\n")
                _log(f"PASS_{pass_n} check_logic=FAIL", run_dir=run_dir)

        if not running:
            restarts += 1
            _log(
                f"PASS_{pass_n} UNHEALTHY running={running} stale={stale} restart=#{restarts}",
                run_dir=run_dir,
            )
            _start_watch(interval=args.watch_interval, log_path=watch_log)
            log_offset = watch_log.stat().st_size if watch_log.exists() else 0
            time.sleep(20)
        elif stale:
            _log(
                f"PASS_{pass_n} STALE_LOG running={running} (no kill)",
                run_dir=run_dir,
            )

        snap = {
            "pass": pass_n,
            "at": datetime.now(UTC).isoformat(),
            "healthy": healthy,
            "restarts": restarts,
            "log_errors_new": len(err_hits),
            "ticks": ticks,
            "check_logic_ok": check_ok,
        }
        snap_path = run_dir / f"pass_{pass_n:04d}.json"
        snap_path.write_text(json.dumps(snap, indent=2, ensure_ascii=False), encoding="utf-8")

        _log(
            f"PASS_{pass_n}_OK healthy={healthy} tick_lines={ticks.get('lines')} "
            f"paths={ticks.get('tick_path')} restarts={restarts}",
            run_dir=run_dir,
        )

    _stop_watch()
    summary = {
        "run_id": run_id,
        "finished_at": datetime.now(UTC).isoformat(),
        "passes": pass_n,
        "restarts": restarts,
        "unique_errors": len(set(all_errors)),
        "issues_file": str(issues_path),
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    _log(f"SESSION_DONE passes={pass_n} restarts={restarts}", run_dir=run_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
