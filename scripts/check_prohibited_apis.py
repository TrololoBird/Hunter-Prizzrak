#!/usr/bin/env python3
"""Fail CI on a private CCXT method call in hunt_core, or stdlib json where serde belongs.

The project is signal-analytics over *public* Binance USDⓈ-M data — it must never
place orders or touch account state. ``ruff``'s ``banned-api`` (TID251) catches banned
*imports* but not *attribute calls* like ``ex.createOrder(...)``, so this scan closes
that gap with a plain source grep (no third-party dependency).

The same scan also guards the orjson migration: ``orjson`` is the declared JSON library
but the whole package once hand-rolled stdlib ``json`` (a crutch duplicating a shipped,
faster library). Every JSON I/O site now goes through :mod:`hunt_core.serde`; this guard
keeps stdlib ``json`` from creeping back into ``hunt_core/``. Ruff's TID251 is repo-wide
and coarse (one rule for every banned import, so exempting tests/research would also lift
the pandas/requests bans there) — a directory-scoped source scan is the precise tool.
The only sanctioned stdlib-``json`` site is the content hash in
``hunt_core/signals/lifecycle.py`` (a persisted ``setup_id`` whose bytes must stay stable);
that file is exempt below.

This script also used to run a drift guard, asserting that ``.github/copilot-instructions.md``
still carried an inline copy of the ban list (Copilot does not follow links). Both that file
and the guard were removed 2026-07-17: only Claude and opencode work on this repo, so the
guard kept a file fresh for a tool nobody runs — upkeep with no reader. The live mirrors are
``CLAUDE.md`` and ``AGENTS.md``; they follow links, so they cite this canon rather than
duplicate it, and there is nothing left to drift.

Canon: docs/ai/rules/prohibited-apis.md.
Run:  uv run python scripts/check_prohibited_apis.py
Exit: 0 = clean, 1 = a prohibited call was found.
"""
from __future__ import annotations

import pathlib
import re
import sys

# Canon list — keep in sync with docs/ai/rules/prohibited-apis.md.
PROHIBITED_METHODS = (
    "createOrder",
    "cancelOrder",
    "editOrder",
    "fetchBalance",
    "fetchPositions",
    "fetchMyTrades",
    "setLeverage",
    "setMarginMode",
    "setPositionMode",
    "withdraw",
    "fetchDeposits",
    "fetchWithdrawals",
)

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SCAN_DIR = _ROOT / "hunt_core"
# Match a method *call* on some object: ``.createOrder(`` — not a substring in a word.
_PATTERN = re.compile(r"\.(" + "|".join(PROHIBITED_METHODS) + r")\s*\(")

# stdlib json → use hunt_core.serde. ``\bjson\.`` won't match inside ``orjson.``.
_JSON_PATTERN = re.compile(r"(^\s*import json\b|\bjson\.(dumps|dump|loads|load|JSONDecodeError)\b)")
# The one sanctioned stdlib-json site (content hash whose bytes must stay stable), plus the
# seam module itself (its docstring names ``json.*`` in prose).
_JSON_EXEMPT = frozenset({"hunt_core/signals/lifecycle.py", "hunt_core/serde.py"})


def _scan_code() -> list[str]:
    violations: list[str] = []
    for path in sorted(_SCAN_DIR.rglob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            m = _PATTERN.search(line)
            if m:
                rel = path.relative_to(_ROOT)
                violations.append(f"{rel}:{lineno}: prohibited CCXT method .{m.group(1)}()")
    return violations


def _scan_json() -> list[str]:
    violations: list[str] = []
    for path in sorted(_SCAN_DIR.rglob("*.py")):
        rel = path.relative_to(_ROOT).as_posix()
        if rel in _JSON_EXEMPT:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _JSON_PATTERN.search(line):
                violations.append(f"{rel}:{lineno}: stdlib json — use hunt_core.serde instead")
    return violations


def main() -> int:
    code_violations = _scan_code()
    if code_violations:
        print("Prohibited private CCXT method calls found:\n", file=sys.stderr)
        for v in code_violations:
            print(f"  {v}", file=sys.stderr)
        print(
            "\nHunt reads public market data only — see docs/ai/rules/prohibited-apis.md.",
            file=sys.stderr,
        )
        return 1
    json_violations = _scan_json()
    if json_violations:
        print("Stdlib json used in hunt_core/ — route JSON I/O through hunt_core.serde:\n", file=sys.stderr)
        for v in json_violations:
            print(f"  {v}", file=sys.stderr)
        return 1
    print(f"OK — no prohibited CCXT calls, no stdlib json in {_SCAN_DIR.relative_to(_ROOT)}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
