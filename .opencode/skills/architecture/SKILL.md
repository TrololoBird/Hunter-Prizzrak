---
name: architecture
description: Use when modifying module boundaries, adding new imports, or checking which modules can depend on each other. Covers dependency graph and isolation rules.
---

# Architecture

## Module dependency graph
```
market/ ──→ data/
market/ ──→ features/
market/ ──→ runtime/
runtime/ ──→ prizrak/ (Deep)
runtime/ ──→ scanner/ (Scanner)
prizrak/ ──→ signals/
scanner/ ──→ signals/
signals/ ──→ deliver/
runtime/ ──→ track/
data/ ────→ features/
```

## Ownership & isolation
| Module | Owns | Must NOT depend on |
|--------|------|-------------------|
| `prizrak/` | Deep engine (pinned majors) | `scanner/` |
| `scanner/` | Universe pre-pump/pre-dump | `prizrak/` |
| `market/` | CCXT client, rate limits, WS/REST | `prizrak/`, `scanner/`, `signals/`, `deliver/` |
| `signals/` | Signal model, dedup, lifecycle | business logic |
| `deliver/` | Telegram formatting, sending | NO business logic, NO pattern detection |
| `features/` | Polars feature engineering | NO architecture decisions |
| `domain/` | Pydantic models only | NO I/O, NO CCXT |
| `toolkit/` | Generic primitives (stats, fusion) | domain-specific code |
| `runtime/` | Cycle loop, orchestration | (orchestrates everything) |
| `track/` | Signal tracker, cooldowns | `prizrak/`, `scanner/` |

## Critical rules
- **Deep and Scanner NEVER import each other**
- **`deliver/` has NO pattern detection or business logic** — purely formatting + sending
- **`features/` does NOT make architectural choices** — caller decides which features
- **`domain/` is pure models** — no I/O, no CCXT, no Telegram
