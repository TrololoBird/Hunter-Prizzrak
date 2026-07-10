# Hunt — crypto-futures signal-analytics

## Project

Standalone Python `>=3.14,<3.15` package — reads public Binance USDⓈ-M data via
**CCXT**, engineers features with **Polars**, delivers manual signals to **Telegram**.

```bash
uv sync                              # install deps
uv run python -m hunt_core watch --interval 30   # production loop
uv run python -m hunt_core watch --once --no-telegram  # smoke test
uv run pytest                        # run tests
uv run ruff check .                  # lint
uv run mypy hunt_core                # type-check
```

**Run first:** copy `.env.example` → `.env`, fill `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID`.

## Layout

```
hunt_core/
  prizrak/      Deep engine (pinned majors, accumulation/POC, ПП break, traps)
  scanner/      Universe-wide pre-pump/pre-dump detection
  toolkit/      Shared primitives (manipulation fusion, order flow, robust stats)
  market/       CCXT client, rate limiting, WS/REST transport
  signals/      Shared spine: Signal, setup_id dedup, lifecycle states
  data/ track/ deliver/ domain/ features/ runtime/ regime/ levels/ ...
config.defaults.toml   # [deep.prizrak] thresholds (config.toml overrides)
tests/                 # pytest test suite
```

## Important conventions

- 100% CCXT market plane. No CoinMarketCap/CoinGecko for market data.
- **Deep** and **Scanner** must stay decoupled — no cross-imports between `prizrak/` and `scanner/`.
- Ruff ignores `E402, E741, F811, F821` project-wide. Don't "fix" them.
- Rely on the skill file at `.opencode/skills/crypto-hunter/SKILL.md` for detailed
  domain knowledge (pattern detection, state machine, delivery pipeline, cooldowns,
  known fixes).

## What's already been fixed

1. **Bullish volume** — checks `z.max()` across whole window
2. **A3 score penalty** — removed `* 0.6` multiplier
3. **Pattern C** — rewritten to single-tick evaluation (no more stale prior_high)
4. **micro_confirmed** — added `ltf_confirmed` param to `_build_setup`
5. **Cooldown testability** — tracker imports at module level
6. **Adaptive stop buffer** — `0.3 × ATR%`, clamped `[1.5%, 5%]`
