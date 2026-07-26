#!/usr/bin/env python3
"""PreToolUse hook: block Edit/Write calls that introduce prohibited APIs.

Mirrors the canon in docs/ai/rules/prohibited-apis.md and scripts/check_prohibited_apis.py,
but runs *before* the write lands instead of catching it later in CI.

Reads the tool-call JSON on stdin (Claude Code PreToolUse hook contract). Exit 2 blocks
the edit and surfaces the message on stderr to the model; exit 0 allows it.
"""
from __future__ import annotations

import json
import os
import re
import sys

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

def _snake(name: str) -> str:
    """camelCase → snake_case: ccxt's Python spelling of the same method."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


# ccxt-python exposes BOTH spellings — verified against a live ``ccxt.binance()`` on 2026-07-26:
# camelCase and snake_case both resolve. snake_case is the idiomatic Python form and the ONLY one
# this codebase uses (measured the same day: 10 snake_case exchange calls, 0 camelCase). Until then
# this guard matched camelCase only — i.e. the spelling nobody here writes — so a planted private
# order call passed both this hook and the CI scan with "OK — no prohibited CCXT calls".
_ALL_SPELLINGS = tuple(dict.fromkeys([*PROHIBITED_METHODS, *(_snake(m) for m in PROHIBITED_METHODS)]))

_CCXT_PATTERN = re.compile(r"\.(" + "|".join(_ALL_SPELLINGS) + r")\s*\(")
_BANNED_IMPORTS = re.compile(r"^\s*(import|from)\s+(pandas|requests|logging)\b", re.MULTILINE)

# These files EXIST to name the prohibited methods — matching them there is the tool refusing to let
# its own definition be edited (hit twice while closing the snake_case hole: once on the scanner,
# once on its test, which must carry the forbidden calls as FIXTURE DATA). The json rule in
# check_prohibited_apis.py already carries the same kind of exemption — this is its CCXT twin.
#
# Deliberately a 3-file allow-list, NOT a blanket `tests/` exemption: test code calling a private
# endpoint for real is exactly as forbidden as production code doing it, and the CI scan does not
# cover tests/ at all, so this hook is the only thing watching them.
_CANON_FILES = (
    "scripts/guard_edit.py",
    "scripts/check_prohibited_apis.py",
    "tests/test_prohibited_api_guard.py",
)


def _is_canon(path: str) -> bool:
    """True for the files whose job is to define the ban list."""
    return any(path.replace(os.sep, "/").endswith(tail) for tail in _CANON_FILES)


def _is_env_file(path: str) -> bool:
    """True for real secret files (.env, foo.env) but not templates (.env.example)."""
    base = os.path.basename(path)
    if base in {".env.example", ".env.sample", ".env.template"}:
        return False
    return base == ".env" or base.endswith(".env")


def _new_content(payload: dict) -> tuple[str, str]:
    tool_input = payload.get("tool_input", {})
    path = tool_input.get("file_path", "")
    if not path.endswith(".py"):
        return path, ""
    content = tool_input.get("content")
    if content is not None:
        return path, content
    return path, tool_input.get("new_string", "") or ""


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0

    raw_path = payload.get("tool_input", {}).get("file_path", "")
    if _is_env_file(raw_path):
        print(f"Blocked edit to {raw_path}:", file=sys.stderr)
        print(
            "  - refusing to write secret file .env via tool. It holds "
            "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID; edit it manually. "
            "Update .env.example (a template, allowed) if you need to document a new key.",
            file=sys.stderr,
        )
        return 2

    path, content = _new_content(payload)
    if not content:
        return 0
    if _is_canon(path):
        return 0

    violations = []
    for m in _CCXT_PATTERN.finditer(content):
        violations.append(f"prohibited private CCXT call .{m.group(1)}()")
    for m in _BANNED_IMPORTS.finditer(content):
        violations.append(f"banned import: {m.group(0).strip()}")

    if violations:
        print(f"Blocked edit to {path}:", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        print(
            "See docs/ai/rules/prohibited-apis.md — this project is public-data "
            "signal-analytics, not a trading bot (no pandas/requests/stdlib logging either).",
            file=sys.stderr,
        )
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
