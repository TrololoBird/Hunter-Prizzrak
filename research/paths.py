"""
Shared path resolution for hunt/research.

Every module MUST use these helpers. No raw f-strings with symbol names in paths.
Dataset versioning is automatic: each fetch bumps the version directory.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

RESEARCH_ROOT = Path(__file__).resolve().parent
REPORTS_DIR = RESEARCH_ROOT / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)


# ── versioning ──────────────────────────────────────────────
def _latest_version() -> int:
    """Find the highest dataset_vN directory number, or 0 if none exist."""
    existing = sorted(RESEARCH_ROOT.glob("dataset_v*"))
    if not existing:
        return 0
    last = existing[-1].name  # dataset_v3
    try:
        return int(last.split("_v")[1])
    except (IndexError, ValueError):
        return 0


def _active_version_file() -> Path:
    return RESEARCH_ROOT / "dataset_active_version.txt"


def get_active_version() -> int:
    """Read the currently active dataset version."""
    vf = _active_version_file()
    if vf.exists():
        try:
            return int(vf.read_text().strip())
        except ValueError:
            pass
    return _latest_version()


def bump_version() -> int:
    """Create a new dataset_vN directory and mark it active. Returns new version number."""
    current = get_active_version()
    new_version = current + 1
    vdir = RESEARCH_ROOT / f"dataset_v{new_version}"
    vdir.mkdir(parents=True, exist_ok=True)
    _active_version_file().write_text(str(new_version))
    return new_version


def ensure_version(version: int | None = None) -> Path:
    """Return (or create) the dataset directory for a given version."""
    if version is None:
        version = get_active_version()
    vdir = RESEARCH_ROOT / f"dataset_v{version}"
    vdir.mkdir(parents=True, exist_ok=True)
    return vdir


def dataset_dir(version: int | None = None) -> Path:
    """Active dataset directory (versioned)."""
    return ensure_version(version)


# ── path helpers ────────────────────────────────────────────
def cache_path(symbol: str, timeframe: str, version: int | None = None) -> Path:
    """Sanitised parquet path: TIA/USDT:USDT + 1h -> dataset_v1/TIA_USDT_USDT_1h.parquet"""
    safe = symbol.replace("/", "_").replace(":", "_")
    return dataset_dir(version) / f"{safe}_{timeframe}.parquet"


def metadata_path(symbol: str, timeframe: str, version: int | None = None) -> Path:
    """Per-file metadata JSON."""
    safe = symbol.replace("/", "_").replace(":", "_")
    return dataset_dir(version) / f"{safe}_{timeframe}_meta.json"


def report_path(name: str) -> Path:
    return REPORTS_DIR / name


# ── metadata helpers ────────────────────────────────────────
def write_metadata(
    symbol: str,
    timeframe: str,
    bars: int,
    first_ts: int,
    last_ts: int,
    duplicates: int,
    gaps: int,
    coverage_pct: float,
    fetch_duration_sec: float,
    version: int | None = None,
) -> Path:
    """Write per-file metadata JSON."""
    import time
    path = metadata_path(symbol, timeframe, version)
    data = {
        "symbol": symbol,
        "exchange": "Binance Futures",
        "timeframe": timeframe,
        "first_bar": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(first_ts / 1000)),
        "last_bar": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(last_ts / 1000)),
        "first_ts": first_ts,
        "last_ts": last_ts,
        "bars": bars,
        "duplicates": duplicates,
        "gaps": gaps,
        "coverage_pct": round(coverage_pct, 2),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "fetch_duration_sec": round(fetch_duration_sec, 1),
    }
    path.write_text(json.dumps(data, indent=2))
    return path
