#!/usr/bin/env python3
"""Fail CI when a private (account/trading) CCXT method is called in hunt_core.

The project is signal-analytics over *public* Binance USDⓈ-M data — it must never
place orders or touch account state. ``ruff``'s ``banned-api`` (TID251) catches banned
*imports* but not *attribute calls* like ``ex.createOrder(...)``, so this scan closes
that gap with a plain source grep (no third-party dependency).

It also guards against instruction drift: ``.github/copilot-instructions.md`` carries an
inline copy of the ban list (Copilot does not follow links), so this script verifies that
copy still mentions every prohibited method in the canon.

Canon: docs/ai/rules/prohibited-apis.md.
Run:  uv run python scripts/check_prohibited_apis.py
Exit: 0 = clean, 1 = a prohibited call was found or copilot-instructions drifted.
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
_COPILOT = _ROOT / ".github" / "copilot-instructions.md"
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


def _check_copilot_drift() -> list[str]:
    """Every canon method must still appear inline in copilot-instructions.md."""
    if not _COPILOT.exists():
        return [f"{_COPILOT.relative_to(_ROOT)}: file missing"]
    text = _COPILOT.read_text(encoding="utf-8")
    return [
        f"{_COPILOT.relative_to(_ROOT)}: missing prohibited method '{m}'"
        for m in PROHIBITED_METHODS
        if m not in text
    ]


def main() -> int:
    code_violations = _scan_code()
    drift = _check_copilot_drift()
    if code_violations:
        print("Prohibited private CCXT method calls found:\n", file=sys.stderr)
        for v in code_violations:
            print(f"  {v}", file=sys.stderr)
        print(
            "\nHunt reads public market data only — see docs/ai/rules/prohibited-apis.md.",
            file=sys.stderr,
        )
    if drift:
        print("\ncopilot-instructions.md drifted from the canon ban list:\n", file=sys.stderr)
        for d in drift:
            print(f"  {d}", file=sys.stderr)
        print(
            "\nRe-sync .github/copilot-instructions.md with docs/ai/rules/prohibited-apis.md.",
            file=sys.stderr,
        )
    if code_violations or drift:
        return 1
    print(f"OK — no prohibited CCXT calls in {_SCAN_DIR.relative_to(_ROOT)}/; copilot list in sync")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
