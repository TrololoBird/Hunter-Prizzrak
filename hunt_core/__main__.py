"""Hunt package CLI — ``python -m hunt_core watch``."""
from __future__ import annotations


import argparse
import sys


def _cmd_watch(argv: list[str]) -> int:
    from hunt_core.bootstrap import bootstrap, require_feature_stack

    bootstrap()
    require_feature_stack()
    from hunt_core._cli import main as _cli_main

    sys.argv = ["hunt_core", "watch"] + argv
    _cli_main()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hunt_core", description="Crypto hunter runtime")
    sub = parser.add_subparsers(dest="command", required=True)

    watch_p = sub.add_parser("watch", help="Minute scanner loop + Telegram")
    watch_p.add_argument("--symbols", nargs="*", default=None)
    watch_p.add_argument("--interval", type=int, default=None)
    watch_p.add_argument("--once", action="store_true")
    watch_p.add_argument("--no-telegram", action="store_true")

    args, rest = parser.parse_known_args(argv)

    # required=True subparsers guarantee args.command == "watch" here.
    watch_argv: list[str] = list(rest)
    if args.symbols is not None:
        watch_argv = ["--symbols", *args.symbols, *watch_argv]
    if args.interval is not None:
        watch_argv.extend(["--interval", str(args.interval)])
    if args.once:
        watch_argv.append("--once")
    if args.no_telegram:
        watch_argv.append("--no-telegram")
    return _cmd_watch(watch_argv)


if __name__ == "__main__":
    raise SystemExit(main())
