---
name: prohibited-api-scan
description: Run the project's prohibited-API scanner (private/trading CCXT method calls) against the working tree, without waiting for CI. Use before committing changes to hunt_core, or when asked to check for banned trading/account API usage.
---

Run the canonical scanner and report its result:

```bash
uv run python scripts/check_prohibited_apis.py
```

It checks one thing: no private/trading CCXT method **calls** (`createOrder`,
`fetchBalance`, `setLeverage`, `withdraw`, … — full list in
docs/ai/rules/prohibited-apis.md) anywhere under `hunt_core/`. Ruff's TID251 bans the
*imports*; only this grep sees an attribute call on an exchange object.

(It used to also assert `.github/copilot-instructions.md` had not drifted from the canon.
That file and the guard were deleted 2026-07-17 — only Claude and opencode work here, and
their instruction files link to the canon instead of duplicating it.)

If it exits non-zero, show the violations verbatim and fix them (or point to the
exact file:line for the user to fix) rather than summarizing them away. This project
is public-data signal-analytics only — it must never place orders or touch account
state.
