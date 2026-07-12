---
name: testing
description: Use when writing or running tests — pytest conventions, async tests with pytest-asyncio, property-based testing with hypothesis, test patterns for Polars/CCXT code.
---

# Testing

## Commands
```bash
uv run pytest          # all tests
uv run pytest -x -q    # fast fail, quiet
uv run pytest -k pattern  # filter by test name
```

## Async tests
```python
import pytest

@pytest.mark.asyncio
async def test_fetch_ticker():
    ticker = await exchange.fetch_ticker("BTC/USDT")
    assert ticker["last"] > 0
```
Configured via `asyncio_mode = "auto"` in `pyproject.toml`.

## Hypothesis (property-based)
```python
from hypothesis import given, strategies as st

@given(st.lists(st.floats(min_value=0.01, max_value=100_000, allow_nan=False)))
def test_indicator_output_in_same_range(prices):
    result = compute_indicator(prices)
    assert result.min() >= min(prices)
    assert result.max() <= max(prices)
```
Use for: indicators, pattern detection, rate limiter, feature engineering.

## Test patterns
- Unit tests for Polars feature functions
- Integration tests via CCXT `--once --no-telegram` smoke run
- Async tests for market data / telegram delivery
- Property-based tests for edge cases (NaN, inf, zero, extreme values)

## Test files
- `tests/test_manipulation_events.py` — primitive-level
- `tests/test_patterns_c.py` — Pattern C logic
- `tests/test_manipulation_delivery_cooldown.py` — cooldown gates
- `tests/test_tracker_entry_zone.py` — tracker logic
- `tests/test_config_and_secrets.py` — config loading
