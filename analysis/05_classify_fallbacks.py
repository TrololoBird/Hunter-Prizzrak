#!/usr/bin/env python3
"""Fallback classification — automated audit of except/return None patterns.

Scans hunt_core/ and classifies every except block and return None line
by severity/quality per Track C3.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
HUNT_CORE = PROJECT / "hunt_core"
REPORT = PROJECT / "data" / "research" / "fallback_audit.json"

SKIP_DIRS = frozenset({"__pycache__", ".mypy_cache", ".pytest_cache"})
SKIP_FILES = frozenset({"__init__.py"})

BROAD_EXCEPTION_NAMES = frozenset({"exception", "baseexception"})

AS_NAME_RE = re.compile(r"\s+as\s+\w+$")
SAME_LINE_BODY_RE = re.compile(
    r"^except\s*(?::\s*(?P<body1>.*)$|(?P<exc>[^:]+):\s*(?P<body2>.*)$)"
)


def _iter_py_files(root: Path):
    for path in root.rglob("*.py"):
        parts = path.relative_to(root).parts
        if any(p in SKIP_DIRS for p in parts):
            continue
        if path.name in SKIP_FILES:
            continue
        yield path


def _indent(line: str) -> int:
    expanded = line.expandtabs(4)
    return len(expanded) - len(expanded.lstrip())


def _parse_except(stripped: str) -> tuple[dict | None, str]:
    """Parse an except line. Returns (parsed_info, inline_body)."""
    if not stripped.startswith("except"):
        return None, ""
    rest = stripped[6:].lstrip()
    if rest.startswith("*"):
        rest = rest[1:].lstrip()
    colon_idx = rest.find(":")
    if colon_idx == -1:
        return None, ""
    exc_part = rest[:colon_idx].strip()
    body_part = rest[colon_idx + 1:].strip()
    if not exc_part:
        return {"type": "bare", "names": []}, body_part
    exc_clean = AS_NAME_RE.sub("", exc_part).strip()
    if exc_clean.startswith("("):
        end = exc_clean.rfind(")")
        inner = exc_clean[1:end] if end > 1 else exc_clean[1:]
        names = [n.strip() for n in inner.split(",") if n.strip()]
    else:
        names = [exc_clean.split()[0]] if exc_clean else []
    if not names:
        return {"type": "bare", "names": []}, body_part
    is_broad = any(n.lower() in BROAD_EXCEPTION_NAMES for n in names)
    return {"type": "broad" if is_broad else "specific", "names": names}, body_part


def _has_log(body_stripped: list[str]) -> bool:
    log_patterns = (
        "logger.", "logging.",
        ".error(", ".warning(", ".exception(", ".critical(",
        ".info(", ".debug(",
    )
    text = " ".join(body_stripped).lower()
    return any(p in text for p in log_patterns)


def _classify_except(parsed: dict, body_lines: list[str]) -> str:
    exc_type = parsed["type"]
    body_stripped = [
        s.lstrip()
        for s in body_lines
        if s.strip() and not s.strip().startswith("#")
    ]
    body_text = " ".join(s.strip() for s in body_stripped)

    has_pass = any(s.strip() == "pass" for s in body_stripped)
    has_continue = any(s.strip() == "continue" for s in body_stripped)
    has_raise = any(s.strip().startswith("raise") for s in body_stripped)
    has_return_none = "return None" in body_text
    has_log = _has_log(body_stripped)

    if exc_type == "bare":
        if has_pass or has_continue or not body_stripped:
            return "MASKED"
        if has_raise:
            return "OK_REHANDLED"
        if has_return_none:
            return "SILENT_FALLBACK"
        if has_log:
            return "LOUD_FALLBACK"
        if any(s.strip() for s in body_stripped):
            return "UNCLASSIFIED"
        return "MASKED"

    if exc_type == "specific":
        return "OK_SPECIFIC"

    if has_raise:
        return "OK_REHANDLED"
    if has_pass or has_continue or not body_stripped:
        return "MASKED"
    if has_return_none:
        return "LOUD_FALLBACK" if has_log else "SILENT_FALLBACK"
    if has_log:
        return "LOUD_FALLBACK"
    return "UNCLASSIFIED"


def scan_file(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {}

    lines = text.splitlines()
    except_blocks: list[dict] = []
    current_except: dict | None = None
    return_none_total = 0
    return_none_in_except = 0

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        indent = _indent(line)

        if current_except is not None and indent <= current_except["indent"]:
            category = _classify_except(
                current_except["parsed"], current_except["body_lines"]
            )
            current_except["category"] = category
            except_blocks.append(current_except)
            current_except = None

        if stripped.startswith("except"):
            parsed, inline_body = _parse_except(stripped)
            if parsed is not None:
                current_except = {
                    "indent": indent,
                    "parsed": parsed,
                    "body_lines": [inline_body] if inline_body else [],
                    "line": stripped[:80],
                }

        if current_except is not None and indent > current_except["indent"]:
            current_except["body_lines"].append(stripped)

        if "return None" in stripped:
            code_part = stripped.split("#")[0].strip()
            if "return None" in code_part:
                return_none_total += 1
                if current_except is not None:
                    return_none_in_except += 1

    if current_except is not None:
        category = _classify_except(
            current_except["parsed"], current_except["body_lines"]
        )
        current_except["category"] = category
        except_blocks.append(current_except)

    category_counts: dict[str, int] = defaultdict(int)
    for blk in except_blocks:
        category_counts[blk["category"]] += 1

    return {
        "except_blocks": [
            {
                "category": b["category"],
                "parsed": b["parsed"],
                "line": b["line"],
            }
            for b in except_blocks
        ],
        "category_counts": dict(category_counts),
        "return_none_total": return_none_total,
        "return_none_in_except": return_none_in_except,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--fix-report", action="store_true", help="write detailed JSON report"
    )
    args = ap.parse_args(argv)

    all_files = sorted(_iter_py_files(HUNT_CORE))
    if not all_files:
        print("No .py files under", HUNT_CORE)
        return 1

    totals: dict[str, int] = defaultdict(int)
    return_none_in_except = 0
    return_none_total = 0
    per_file: dict[str, dict] = {}

    for fpath in all_files:
        rel = fpath.relative_to(PROJECT)
        result = scan_file(fpath)
        if not result:
            continue
        for cat, count in result["category_counts"].items():
            totals[cat] += count
        return_none_in_except += result["return_none_in_except"]
        return_none_total += result["return_none_total"]
        per_file[str(rel)] = result

    non_ok_categories = {"MASKED", "SILENT_FALLBACK", "LOUD_FALLBACK", "UNCLASSIFIED"}
    file_non_ok = []
    for rel_path, data in per_file.items():
        non_ok = sum(
            c for cat, c in data["category_counts"].items()
            if cat in non_ok_categories
        )
        if non_ok > 0:
            rel_short = rel_path.replace("hunt_core/", "")
            file_non_ok.append((rel_short, non_ok))

    file_non_ok.sort(key=lambda x: -x[1])
    total_except = sum(totals.values())

    print(
        "\u2550" * 12,
        "FALLBACK CLASSIFICATION REPORT \u2014 per plan Track C3",
        "\u2550" * 12,
        sep="",
    )
    print()

    print("EXCEPT PATTERNS:")
    cats = [
        ("MASKED (silent)", totals.get("MASKED", 0)),
        ("SILENT_FALLBACK (no log)", totals.get("SILENT_FALLBACK", 0)),
        ("LOUD_FALLBACK (logged)", totals.get("LOUD_FALLBACK", 0)),
        ("OK_SPECIFIC", totals.get("OK_SPECIFIC", 0)),
        ("OK_REHANDLED", totals.get("OK_REHANDLED", 0)),
        ("UNCLASSIFIED", totals.get("UNCLASSIFIED", 0)),
    ]
    for label, count in cats:
        print(f"  {label:25s} {count:>6d}")
    print(f"  {'TOTAL':25s} {total_except:>6d}")
    print()

    print("RETURN NONE:")
    print(f"  {'EXCEPT_FALLBACK':25s} {return_none_in_except:>6d}")
    print(f"  {'NORMAL':25s} {return_none_total - return_none_in_except:>6d}")
    print(f"  {'TOTAL':25s} {return_none_total:>6d}")
    print()

    print("Top-5 files by non-OK except:")
    for i, (fname, count) in enumerate(file_non_ok[:5], 1):
        print(f"  {i}. {fname:35s} {count}")

    if args.fix_report:
        report_data = {
            "summary": {
                "except_patterns": {k: v for k, v in cats},
                "total_except": total_except,
                "return_none": {
                    "EXCEPT_FALLBACK": return_none_in_except,
                    "NORMAL": return_none_total - return_none_in_except,
                    "TOTAL": return_none_total,
                },
                "top_5_non_ok": [
                    {"file": f, "count": c} for f, c in file_non_ok[:5]
                ],
            },
            "per_file": per_file,
        }
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(report_data, indent=2, default=str))
        print(f"\nReport written \u2192 {REPORT}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
