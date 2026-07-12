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

_CCXT_PATTERN = re.compile(r"\.(" + "|".join(PROHIBITED_METHODS) + r")\s*\(")
_BANNED_IMPORTS = re.compile(r"^\s*(import|from)\s+(pandas|requests|logging)\b", re.MULTILINE)


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
