---
name: config
description: Use when modifying configuration — config.toml, config.defaults.toml, .env secrets, settings merge logic, PrizrakConfig.
---

# Configuration

## Files
| File | Purpose | Git |
|------|---------|-----|
| `config.defaults.toml` | Single source of truth for thresholds | ✅ tracked |
| `config.toml` | Only `[bot]`/`[bot.network]` overrides (proxy) | ❌ gitignored |
| `.env` | Secrets: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID | ❌ gitignored |
| `.env.example` | Template with placeholder values | ✅ tracked |

## Merge logic
1. `load_settings()` (`domain/config.py`) reads `config.toml`
2. Only `[bot]` / `[bot.network]` table from config.toml is merged
3. Dotenv variables (`python-dotenv`) provide TELEGRAM_* and proxy secrets
4. Threshold sections (`[hunter]`, `[watch]`, `[levels]`, etc.) are NEVER overridden by config.toml
5. `PrizrakConfig.load()` reads `[deep.prizrak]` from `config.defaults.toml` only

## Rules
- Never add threshold overrides to `config.toml` — they're silently ignored
- Never commit `.env` — it contains secrets
- Use `config.defaults.toml` for all default values
