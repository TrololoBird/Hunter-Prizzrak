"""Data ingest, universe, lake store, tick I/O, collection."""

from hunt_core.data.lake import (
    FeatureLakeWriter,
    LakeDataError,
    LakeStore,
    append_feature_row,
    append_tick_rows,
    get_feature_lake_writer,
    import_ticks_to_lake,
    query_baseline_stats,
    query_features,
    read_features,
    serialize_tick_row,
)

__all__ = [
    "FeatureLakeWriter",
    "LakeDataError",
    "append_feature_row",
    "get_feature_lake_writer",
    "query_baseline_stats",
    "query_features",
    "read_features",
    "LakeStore",
    "import_ticks_to_lake",
    "append_tick_rows",
    "serialize_tick_row",
    "snapshot_symbol",
    "PrescanEngine",
    "UniverseConfig",
    "apply_quality_gates",
    "funnel_hot_candidates",
    "prescan_from_tickers",
]


def __getattr__(name: str):
    if name == "snapshot_symbol":
        from hunt_core.runtime.tick_assembly import snapshot_symbol

        return snapshot_symbol
    if name in {
        "PrescanEngine",
        "UniverseConfig",
        "apply_quality_gates",
        "funnel_hot_candidates",
        "prescan_from_tickers",
    }:
        from hunt_core.data import collect as _collect

        return getattr(_collect, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
