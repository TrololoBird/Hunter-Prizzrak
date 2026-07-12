---
name: prohibited-api-scan
description: Run the project's prohibited-API scanner (private CCXT calls, copilot-instructions drift) against the working tree, without waiting for CI. Use before committing changes to hunt_core, or when asked to check for banned trading/account API usage.
---

Run the canonical scanner and report its result:

```bash
uv run python scripts/check_prohibited_apis.py
```

This checks two things:
1. No private/trading CCXT method calls (`createOrder`, `fetchBalance`,
   `setLeverage`, `withdraw`, etc. — full list in
   docs/ai/rules/prohibited-apis.md) anywhere under `hunt_core/`.
2. `.github/copilot-instructions.md` hasn't drifted from the canon ban list.

If it exits non-zero, show the violations verbatim and fix them (or point to the
exact file:line for the user to fix) rather than summarizing them away. This project
is public-data signal-analytics only — it must never place orders or touch account
state.
