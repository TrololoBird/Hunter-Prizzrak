#!/usr/bin/env bash
# Refresh CCXT Python AI skill for Claude Code + OpenCode.
# Runs automatically on `uv sync` via pyproject.toml [tool.uv.scripts.sync].
set -euo pipefail

SRC="https://raw.githubusercontent.com/ccxt/ccxt/master/.claude/skills/ccxt-python/SKILL.md"
DEST1="${HOME}/.opencode/skills/ccxt-python/SKILL.md"
DEST2="${HOME}/.claude/skills/ccxt-python/SKILL.md"

mkdir -p "$(dirname "$DEST1")" "$(dirname "$DEST2")"
if curl -fsSL "$SRC" -o "$DEST1" && cp "$DEST1" "$DEST2"; then
    echo "CCXT Python skill refreshed: $(wc -c < "$DEST1") bytes"
else
    echo "WARNING: failed to refresh CCXT Python skill (offline or no internet)" >&2
fi
