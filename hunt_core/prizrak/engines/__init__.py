"""Surviving Deep-delivery infrastructure, agnostic to which engine produced the
verdict. ``orchestrator.py`` (L0-L5 scenario engine, "Verdict V2") was deleted —
the PrizrakTrade methodology engine (``hunt_core.prizrak.orchestrator``) is now the
sole decision authority and fills the same ``row["prizrak_summary"]`` dict
contract these modules already consumed generically.
"""
