#!/usr/bin/env python3
"""Fail CI when a private (account/trading) CCXT method is called in hunt_core.

The project is signal-analytics over *public* Binance USDⓈ-M data — it must never
place orders or touch account state. ``ruff``'s ``banned-api`` (TID251) catches banned
*imports* but not *attribute calls* like ``ex.createOrder(...)``, so this scan closes
that gap with a plain source grep (no third-party dependency).

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


def _scan_code() -> list[str]:
    violations: list[str] = []
    for path in sorted(_SCAN_DIR.rglob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            m = _PATTERN.search(line)
            if m:
                rel = path.relative_to(_ROOT)
                violations.append(f"{rel}:{lineno}: prohibited CCXT method .{m.group(1)}()")
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
    print(f"OK — no prohibited CCXT calls in {_SCAN_DIR.relative_to(_ROOT)}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
