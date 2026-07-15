---
name: phantom-key-scan
description: Audit the working tree (or a given set of files) for the project's signature defect class — phantom dict keys, falsy-zero chains, orphan fields, name-lies, dead code, config drift. Combines the fast static pass (vulture) with the LLM auditors. Use before committing a substantial change to hunt_core, or when asked to "scan for phantom keys / dead code / config drift".
---

Two-layer scan for the "unknown treated as zero/fact" defect family (invariant I-6).

## 1. Fast static pass — dead code (vulture)

```bash
uv run vulture   # reads [tool.vulture] in pyproject: hunt_core + whitelist, min-confidence 80
```

A clean exit means no new dead code. New hits are real (conf 80 is signal-only) — fix them
or, for a reviewed intentional case (a signature-contract param), append the symbol to
`.vulture_whitelist.py` with a one-line reason. Do NOT drop min-confidence below 80
(conf 60 ≈ 396 mostly-false hits).

## 2. Reasoning pass — phantom keys / falsy-zero / config drift (subagents)

A linter can't tell an internal dict (producer in this repo) from an external one (CCXT /
TOML / JSON), so dispatch the LLM auditors over the CHANGED files:

- **phantom-key-auditor** — phantom keys, falsy-zero `or`-chains, orphan fields, name-lies.
  Give it the diff or the changed `hunt_core/**` files.
- **config-drift-auditor** — run it when the change touched `*.toml`, `.env*`, or a
  `config.py` loader: dead sections, doc-only keys, section-name mismatches.

Launch them with the Agent tool (they are read-only, report `file:line` + grep proof +
minimal fix). Scope them to the diff, not the whole repo, to stay cheap.

## 3. Report

Collate: vulture dead-code + auditor findings. For each, state class / `file:line` / proof /
fix, and whether the fix is behaviour-preserving (delete dead code / annotate) or
emission-changing (revive a wanted-but-unwired feature → backtest-gated, see the
`backtest-gate` skill). Do not re-flag closed gaps (spec §8 G-1..G-30, AUDIT_ROUND2.md).
