---
name: telegram
description: Use when sending Telegram messages or working with the deliver module — aiogram setup, HTML formatting, error handling, security rules.
---

# Telegram (aiogram)

## Bot setup
```python
from aiogram import Bot
bot = Bot(token=TELEGRAM_BOT_TOKEN)
```

## Sending signals
```python
from aiogram.enums import ParseMode
await bot.send_message(
    chat_id=TELEGRAM_CHAT_ID,
    text=message_html,
    parse_mode=ParseMode.HTML,
)
```

## Responsibilities
- `deliver/` module handles ALL Telegram interaction
- Signal formatting in Telegram HTML style
- Error handling: catch aiogram exceptions, log via structlog

## Security
- Token in `.env` only — never hardcoded
- Never commit `TELEGRAM_BOT_TOKEN` or `TELEGRAM_CHAT_ID`
