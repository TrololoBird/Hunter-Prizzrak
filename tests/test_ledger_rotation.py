"""Candidate-ledger rotation pins.

The B4 ledger grows by design ("tens of GB/month at scale" — module docstring),
but it was unbounded: live it reached 7.7 GB in a single file that
``load_pending_backfill`` reads whole. Rotation archives, never deletes.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

from hunt_core.track.candidate_ledger import rotate_ledger_if_large


def _write_rows(p: Path, n: int) -> list[dict[str, str]]:
    rows = [{"candidate_id": f"c{i}", "record_kind": "decision"} for i in range(n)]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return rows


def test_no_rotation_below_threshold(tmp_path: Path) -> None:
    p = tmp_path / "ledger.jsonl"
    _write_rows(p, 3)
    assert rotate_ledger_if_large(path=p, max_bytes=10_000) is None
    assert p.exists() and p.read_text().count("\n") == 3


def test_missing_file_is_noop(tmp_path: Path) -> None:
    assert rotate_ledger_if_large(path=tmp_path / "nope.jsonl", max_bytes=1) is None


def test_rotation_archives_every_row_and_deletes_nothing(tmp_path: Path) -> None:
    p = tmp_path / "ledger.jsonl"
    rows = _write_rows(p, 50)

    archive = rotate_ledger_if_large(path=p, max_bytes=10)
    assert archive is not None
    assert archive.suffixes[-2:] == [".jsonl", ".gz"]
    assert archive.parent == tmp_path / "archive"

    # every observation survives, byte-for-byte
    with gzip.open(archive, "rt", encoding="utf-8") as fh:
        restored = [json.loads(line) for line in fh if line.strip()]
    assert restored == rows

    # the live ledger is gone (the next append recreates it empty)
    assert not p.exists()
    # no .tmp staging left behind
    assert list((tmp_path / "archive").glob("*.tmp")) == []


def test_rotation_leaves_a_writable_fresh_ledger(tmp_path: Path) -> None:
    p = tmp_path / "ledger.jsonl"
    _write_rows(p, 20)
    rotate_ledger_if_large(path=p, max_bytes=10)

    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"candidate_id": "fresh"}) + "\n")
    assert json.loads(p.read_text().strip())["candidate_id"] == "fresh"
