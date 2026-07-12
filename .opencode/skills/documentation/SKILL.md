---
name: documentation
description: Use when writing code documentation or reviewing code style — Google-style docstrings, type hints, naming conventions, anti-patterns to avoid.
---

# Documentation & code style

## Docstrings (Google-style)
```python
def func(arg1: str, arg2: int) -> bool:
    """Short description.

    Optional longer description.

    Args:
        arg1: description
        arg2: description

    Returns:
        description

    Raises:
        ValueError: when ...
    """
```

## Type hints
- Every function MUST have complete type hints
- Use `TYPE_CHECKING` for circular imports
- Avoid `Any` — prefer `dict[str, Any]` over bare `dict`

## Naming
- Module-level constants: `UPPER_CASE`
- Private helpers: `_` prefix
- Descriptive names, no abbreviations (except well-known: OI, ATR, EMA)

## Anti-patterns
- No magic numbers — define named constants
- No commented-out code — delete it
- No `# type: ignore` or `noqa` without explanation
- No utility files with 1-2 functions — put in existing modules
