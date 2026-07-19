"""serde — the orjson JSON seam (replaces stdlib json). Proves stdlib-compatible semantics."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from hunt_core import serde


def test_roundtrip_scalars_and_containers() -> None:
    obj = {"a": 1, "b": 2.5, "c": "x", "d": [1, 2, 3], "e": None, "f": True}
    assert serde.loads(serde.dumps(obj)) == obj
    assert serde.loads(serde.dumps_str(obj)) == obj


def test_dumps_str_is_str_dumps_is_bytes() -> None:
    assert isinstance(serde.dumps({"a": 1}), bytes)
    assert isinstance(serde.dumps_str({"a": 1}), str)


def test_datetime_passthrough_matches_stdlib_default_str() -> None:
    # orjson would natively emit RFC-3339 ("T" separator); OPT_PASSTHROUGH_DATETIME routes it
    # through default=str so the ledger byte-format is unchanged from the old json.dumps(default=str).
    dt = datetime(2026, 7, 18, 12, 34, 56, tzinfo=UTC)
    row = {"ts": dt}
    # Same value format as stdlib default=str (compact token separators aside): a space-separated
    # "2026-07-18 12:34:56+00:00", NOT orjson's native RFC-3339 "2026-07-18T12:34:56+00:00".
    assert serde.dumps_str(row) == json.dumps({"ts": str(dt)}, separators=(",", ":"))
    assert serde.loads(serde.dumps(row))["ts"] == "2026-07-18 12:34:56+00:00"


def test_default_str_fallback_for_decimal() -> None:
    assert serde.loads(serde.dumps({"x": Decimal("1.5")})) == {"x": "1.5"}


def test_indent_and_sort_keys() -> None:
    pretty = serde.dumps_str({"b": 1, "a": 2}, indent=True, sort_keys=True)
    assert pretty == '{\n  "a": 2,\n  "b": 1\n}'


def test_non_ascii_preserved() -> None:
    assert serde.loads(serde.dumps_str({"k": "Призрак"})) == {"k": "Призрак"}


def test_loads_accepts_bytes_and_str() -> None:
    assert serde.loads(b'{"a":1}') == {"a": 1}
    assert serde.loads('{"a":1}') == {"a": 1}


def test_invalid_json_raises_jsondecodeerror() -> None:
    with pytest.raises(serde.JSONDecodeError):
        serde.loads("{not json}")
    # subclass of the stdlib error, so pre-existing `except json.JSONDecodeError` still catches it
    assert issubclass(serde.JSONDecodeError, json.JSONDecodeError)
