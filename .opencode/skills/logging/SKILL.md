---
name: logging
description: Use when adding logging to any module — structlog setup, structured key=value pairs, log levels, forbidden patterns.
---

# Logging (structlog)

## Pre-bound loggers
```python
import structlog
LOG = structlog.get_logger(__name__)
```

## Structured keys — NO f-strings in messages
```python
# GOOD
LOG.info("symbol_fetched", symbol="BTCUSDT", elapsed_s=0.3)

# BAD
LOG.info(f"symbol_fetched {symbol}")  # no key=value
```

## Levels
- `LOG.debug()` — pacing, verbose, high-frequency
- `LOG.info()` — normal operations
- `LOG.warning()` — recoverable issues
- `LOG.error()` — failures, exceptions

## Rules
- NO stdlib `logging` module — always through structlog
- NO printing or logging secrets, API keys, tokens
