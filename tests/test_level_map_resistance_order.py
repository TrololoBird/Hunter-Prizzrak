"""Level-map single levels render nearest-to-price first, deduped, per TF.

Two grid entries for the same TF (donchian + Prizrak structure) used to print
resistances in grid order, not by proximity — e.g. «сопротивл=63527.6,
сопротивл=63362.4» with the farther one first. Support must be highest-first
(nearest below), resistance lowest-first (nearest above).
"""
from __future__ import annotations

from hunt_core.deliver.confluence_grid import format_grid_telegram


def _line(text: str, tf: str) -> str:
    return next((ln for ln in text.splitlines() if ln.startswith(f"· {tf}:")), "")


def test_resistances_nearest_first_and_deduped() -> None:
    grid = [
        {"tf": "1h", "support": 61806.0, "resistance": 63527.6},
        {"tf": "1h", "resistance": 63362.4},   # nearer — must come first
        {"tf": "1h", "resistance": 63527.6},   # duplicate — must dedup
    ]
    line = _line(format_grid_telegram(grid, price=62077.6), "1h")
    i_near = line.find("63362.4")
    i_far = line.find("63527.6")
    assert i_near != -1 and i_far != -1
    assert i_near < i_far, f"nearer resistance must print first: {line}"
    assert line.count("63527.6") == 1, f"duplicate not deduped: {line}"


def test_supports_nearest_first() -> None:
    grid = [
        {"tf": "4h", "support": 60000.0},
        {"tf": "4h", "support": 61500.0},  # nearer (higher) — must come first
    ]
    line = _line(format_grid_telegram(grid, price=62000.0), "4h")
    assert line.find("61500") < line.find("60000"), line


def test_multi_tf_level_surfaced_as_confluence() -> None:
    # 57758.6 as BOTH 1w and 1d support is stronger than a single-TF level — it must
    # be surfaced as a «мульти-ТФ конфлюенс» highlight, not just buried in two lines.
    grid = [
        {"tf": "1w", "support": 57758.6},
        {"tf": "1d", "support": 57758.6},
        {"tf": "4h", "support": 61806.0},  # single-TF → NOT in the confluence line
    ]
    text = format_grid_telegram(grid, price=62000.0)
    conf = next((ln for ln in text.splitlines() if "мульти-ТФ конфлюенс" in ln), "")
    assert "57758.6" in conf and "1d+1w" in conf
    assert "61806.0" not in conf  # single-TF level not flagged as confluence


def test_no_confluence_line_when_no_shared_levels() -> None:
    grid = [{"tf": "1h", "support": 61806.0, "resistance": 63000.0}]
    text = format_grid_telegram(grid, price=62000.0)
    assert "мульти-ТФ конфлюенс" not in text


def test_deeper_list_kept_in_upstream_order() -> None:
    # List-valued kinds (глубже/выше) keep their upstream order, not re-sorted.
    grid = [{"tf": "глубже", "support": [61520.0, 61297.0, 59800.0]}]
    line = _line(format_grid_telegram(grid, price=62000.0), "глубже")
    assert line.find("61520") < line.find("61297") < line.find("59800"), line
