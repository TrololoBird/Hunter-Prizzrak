"""Module 2 — Hunter (Охотник): universe scan for manipulation formations.

Two parts:
- ``detect/`` — the sole signal path: a persistent, incremental per-symbol
  state machine (Pattern A/A3 long pumps, Pattern B short dumps).
- ``prescan.py`` — cheap universe pre-filter that builds the watchlist the
  detect path runs on.

The legacy fusion/gate/playbook/catalog/arbiter stack was deleted — it was
superseded by ``detect/patterns.advance_manipulation_state`` +
``deliver/manipulation_delivery.py`` and had no live callers.
"""
MODULE_ID = 2
MODULE_NAME = "scanner"

__all__ = ["MODULE_ID", "MODULE_NAME"]
