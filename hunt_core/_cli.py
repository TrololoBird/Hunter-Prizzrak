"""Hunter CLI — lock, signals, argparse (thin app shell)."""
from __future__ import annotations


import argparse
import asyncio
import os
import signal

from hunt_core.bootstrap import bootstrap, require_feature_stack

bootstrap()
require_feature_stack()

from hunt_core.runtime.cycle import run_loop
from hunt_core.runtime.state import request_stop
from hunt_core.data.universe import DEFAULT_SYMBOLS


def _on_signal(*_args: object) -> None:
    request_stop()


def _acquire_single_instance_lock() -> None:
    from hunt_core.paths import DATA

    lock = DATA / "watch.pid"
    supervised_child = os.environ.get("HUNT_SUPERVISED_CHILD") == "1"
    if lock.exists():
        try:
            other = int(lock.read_text(encoding="utf-8").strip() or "0")
        except (OSError, ValueError):
            other = 0
        if other and other != os.getpid():
            alive = False
            try:
                os.kill(other, 0)
                alive = True
            except ProcessLookupError:
                alive = False
            except PermissionError:
                alive = True
            if alive and not supervised_child:
                raise SystemExit(
                    f"hunt_core watch already running (pid={other}); refusing to start a second writer. "
                    f"Kill it first or remove {lock} if stale."
                )
            if alive and supervised_child:
                try:
                    os.kill(other, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(os.getpid()), encoding="utf-8")


def _normalize_cli_symbols(raw: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    out: list[str] = []
    for item in raw or ():
        for part in str(item).replace(",", " ").split():
            sym = part.strip().upper()
            if sym and sym not in out:
                out.append(sym)
    return tuple(out)


def main() -> None:
    parser = argparse.ArgumentParser(description="Hunter CLI — watch")
    sub = parser.add_subparsers(dest="command")

    # watch (default)
    watch_parser = sub.add_parser("watch", help="Run signal watch loop (default)")
    watch_parser.add_argument(
        "--symbols",
        nargs="*",
        default=list(DEFAULT_SYMBOLS),
        help="CLI extras on top of anchors BTC ETH XAU XAG + scanner watchlist",
    )
    watch_parser.add_argument("--interval", type=int, default=30)
    watch_parser.add_argument("--once", action="store_true")
    watch_parser.add_argument("--no-telegram", action="store_true", help="Log only, no Telegram sends")

    args = parser.parse_args()

    # Default: watch
    symbol_list = _normalize_cli_symbols(args.symbols) if hasattr(args, "symbols") else tuple(DEFAULT_SYMBOLS)
    interval_s = getattr(args, "interval", 30)
    once = getattr(args, "once", False)
    no_tg = getattr(args, "no_telegram", False)

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    if not once:
        _acquire_single_instance_lock()
    asyncio.run(
        run_loop(
            symbol_list,
            interval_s,
            once,
            send_telegram=not no_tg,
        )
    )


if __name__ == "__main__":
    main()
