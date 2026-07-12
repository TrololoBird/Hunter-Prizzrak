---
name: smoke
description: Run the canonical Hunt smoke test — one full watch cycle over live public Binance data with no Telegram delivery. Use to verify the pipeline still builds features, detects setups, and renders output after a change.
disable-model-invocation: true
---

Run one full pipeline cycle without sending anything to Telegram:

```bash
uv run python -m hunt_core watch --once --no-telegram
```

This exercises the whole chain end-to-end: CCXT public data fetch → Polars feature
engine → scanner/prizrak detection → delivery rendering (stdout, not Telegram).

- Exit 0 with rendered output = pipeline healthy.
- Non-zero exit or a traceback = show it verbatim and diagnose; do not summarize the
  error away.
- Network/data-readiness warnings (proxy preflight, universe_health) are expected on a
  cold run — call them out but don't treat them as pipeline failures unless the cycle
  aborts.

For a deeper check after touching feature/signal code, also run the pinned invariants:

```bash
uv run pytest tests/test_signal_invariants.py -q
```
