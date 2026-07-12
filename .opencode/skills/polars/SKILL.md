---
name: polars
description: Use when writing Polars DataFrame operations, LazyFrame pipelines, feature engineering, or any data transformation. No pandas, Expression API only.
---

# Polars — data tool

## Golden rules
- **NO pandas.** Zero exceptions.
- **NO `iter_rows()`** — use vectorized expressions
- **NO Python `for` loops over rows** — use `map_elements()` or expressions
- **NO `to_dicts()`** for iteration — use Polars expressions
- **NO `list(dict.keys())`** — unnecessary materialization

## Expression API (preferred)
```python
pl.col("close")
pl.when(pl.col("volume") > 0).then(...).otherwise(...)
pl.col("close").shift(1)
pl.col("close").rolling_mean(window_size=14)
pl.col("close").pct_change()
```
Use `.alias("new_name")` to rename expressions.

## LazyFrame
```python
df.lazy() \
  .filter(...) \
  .with_columns(...) \
  .group_by("symbol") \
  .agg(...) \
  .collect()
```
- Chain transformations with `.lazy()`, call `.collect()` once
- Avoid `.collect()` in hot loops — use expressions

## Type safety
```python
pl.Series("kama10", values, dtype=pl.Float64)
```
- Always specify `dtype` on `pl.Series()` — avoids inference edge cases

## Extensions used
- `polars_ta` — TA indicators
- `polars_ols` — rolling OLS
- `polars_ds` — data science (entropy, KS test)
- `polars-trading` — removed (native Polars fallbacks exist)
