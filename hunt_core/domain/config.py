"""Hunt runtime settings — standalone, no bot catalog."""
from __future__ import annotations



import os
import tomllib as _toml_lib
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from hunt_core.secrets import load_secrets, parse_operator_user_ids

# Strategy catalog removed with the legacy detection stack; the fusion engine has no
# per-strategy setup ids. Empty so config validation accepts no [setups.*] entries.
HUNT_SETUP_IDS: tuple[str, ...] = ()

REQUIRED_PINNED_SYMBOLS: tuple[str, ...] = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "XAUUSDT",
    "XAGUSDT",
    "PAXGUSDT",
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class RuntimeConfig(_StrictModel):
    strict_data_quality: bool = True
    shortlist_unified_routing: bool = False
    analysis_kline_intervals: tuple[str, ...] = ("5m", "15m", "1h", "4h", "1d")
    log_level: str = "INFO"
    telemetry_subdir: str = "telemetry"

    @field_validator("analysis_kline_intervals", mode="before")
    @classmethod
    def _normalize_intervals(cls, value: object) -> tuple[str, ...]:
        if value is None:
            return ("5m", "15m", "1h", "4h", "1d")
        if isinstance(value, str):
            return (value.strip(),)
        if isinstance(value, (list, tuple, set)):
            return tuple(str(item).strip() for item in value if str(item).strip())
        return ("5m", "15m", "1h", "4h", "1d")


class NetworkConfig(_StrictModel):
    proxy_url: str | None = None
    proxy_urls: list[str] = Field(default_factory=list)

    @field_validator("proxy_url")
    @classmethod
    def _normalize_proxy(cls, value: str | None) -> str | None:
        raw = str(value or "").strip()
        return raw or None

    @field_validator("proxy_urls", mode="before")
    @classmethod
    def _normalize_list(cls, value: object) -> list[str]:
        out: list[str] = []
        for raw in value or ():
            item = str(raw or "").strip()
            if item and item not in out:
                out.append(item)
        return out

    def effective_proxy_urls(self) -> list[str]:
        out: list[str] = []
        for raw in (self.proxy_url, *self.proxy_urls):
            item = str(raw or "").strip()
            if item and item not in out:
                out.append(item)
        return out


class FilterConfig(_StrictModel):
    min_score: float = Field(default=0.60, ge=0.0, le=1.0)
    cooldown_minutes: int = Field(default=60, ge=0, le=1440)
    min_bars_15m: int = Field(default=500, ge=30, le=5000)
    min_bars_1h: int = Field(default=300, ge=30, le=5000)
    min_bars_5m: int = Field(default=200, ge=30, le=5000)
    min_bars_4h: int = Field(default=250, ge=30, le=5000)
    setups: dict[str, dict[str, float]] = Field(default_factory=dict)


class DeliveryConfig(_StrictModel):
    watch_min_score: float = Field(default=0.0, ge=0.0, le=1.0)


class TrackingConfig(_StrictModel):
    pending_expiry_minutes: int = Field(default=180, ge=5, le=10080)
    late_entry_chase_pct: float = Field(default=0.002, ge=0.0001, le=0.05)


class NotifierConfig(_StrictModel):
    provider: str = "telegram"


class WSConfig(_StrictModel):
    kline_intervals: tuple[str, ...] = ("5m", "15m", "1h", "4h")

    @field_validator("kline_intervals", mode="before")
    @classmethod
    def _normalize_klines(cls, value: object) -> tuple[str, ...]:
        if isinstance(value, str):
            return (value.strip(),)
        if isinstance(value, (list, tuple, set)):
            return tuple(str(item).strip() for item in value if str(item).strip())
        return ("5m", "15m", "1h", "4h")


class AssetConfig(_StrictModel):
    primary_timeframe: Literal["5m", "15m", "1h", "4h"] = "15m"
    context_timeframes: tuple[Literal["5m", "15m", "1h", "4h"], ...] = ("1h", "4h")
    excluded_strategies: tuple[str, ...] = ()
    allowed_strategies: tuple[str, ...] = ()
    analyst: bool = False


class SetupConfig(_StrictModel):
    dump_initiation: bool = True
    squeeze_expansion: bool = True
    liquidity_sweep: bool = True
    bos_choch: bool = True
    value_accept_reject: bool = True
    oi_cascade: bool = True
    accumulation_breakout: bool = True

    def enabled_setup_ids(self) -> tuple[str, ...]:
        return tuple(sid for sid in HUNT_SETUP_IDS if bool(getattr(self, sid, False)))


class HuntSettings(_StrictModel):
    tg_token: str = ""
    target_chat_id: str = ""
    operator_user_ids: tuple[int, ...] = ()
    data_dir: Path = Path("data") / "bot"
    config_path: Path = Path("config.toml")
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    filters: FilterConfig = Field(default_factory=FilterConfig)
    tracking: TrackingConfig = Field(default_factory=TrackingConfig)
    setups: SetupConfig = Field(default_factory=SetupConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    ws: WSConfig = Field(default_factory=WSConfig)
    delivery: DeliveryConfig = Field(default_factory=DeliveryConfig)
    notifiers: NotifierConfig = Field(default_factory=NotifierConfig)
    assets: dict[str, AssetConfig] = Field(default_factory=dict)
    # Maps runtime config is loaded via hunt_core.maps.config.load_maps_config()
    # (full 18-field TOML [maps] + HUNT_MAPS_* env), the single source of truth.

    @property
    def telemetry_dir(self) -> Path:
        return self.data_dir / self.runtime.telemetry_subdir

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @field_validator("operator_user_ids", mode="before")
    @classmethod
    def _normalize_ops(cls, value: object) -> tuple[int, ...]:
        if value is None:
            return ()
        if isinstance(value, int):
            return (value,)
        if isinstance(value, str):
            return parse_operator_user_ids(value)
        if isinstance(value, (list, tuple, set)):
            ids: list[int] = []
            for item in value:
                try:
                    ids.append(int(item))
                except (TypeError, ValueError):
                    continue
            return tuple(sorted(set(ids)))
        return ()

    @model_validator(mode="after")
    def _normalize_assets(self) -> HuntSettings:
        self.assets = {
            str(symbol).strip().upper(): config for symbol, config in self.assets.items()
        }
        return self

    def validate_for_runtime(self, *, require_telegram: bool) -> None:
        unknown = sorted(set(self.filters.setups) - set(HUNT_SETUP_IDS))
        if unknown:
            raise ValueError(f"unknown setup overrides: {unknown}")
        unknown_enabled = sorted(set(self.setups.enabled_setup_ids()) - set(HUNT_SETUP_IDS))
        if unknown_enabled:
            raise ValueError(f"unknown enabled setups: {unknown_enabled}")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if require_telegram:
            if not self.tg_token.strip():
                raise ValueError("TELEGRAM_BOT_TOKEN is required for runtime")
            if not self.target_chat_id.strip():
                raise ValueError("TELEGRAM_CHAT_ID is required for runtime")


# Back-compat alias used by schemas and data_readiness
BotSettings = HuntSettings


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        parsed = _toml_lib.load(handle)
    return parsed if isinstance(parsed, dict) else {}


def _resolve_config_source(config_file: Path) -> Path:
    if config_file.is_file():
        return config_file

    repo_root = Path(__file__).resolve().parents[2]
    for candidate in (
        repo_root / config_file.name,
        repo_root / "config.toml",
        repo_root / "config.defaults.toml",
        Path.cwd() / config_file.name,
        Path.cwd() / "config.toml",
        Path.cwd() / "config.defaults.toml",
    ):
        if candidate.is_file():
            return candidate

    example = config_file.with_name("config.toml.example")
    if example.is_file():
        return example

    for base in (Path.cwd(), *Path.cwd().parents):
        candidate = base / "config.toml.example"
        if candidate.is_file():
            return candidate
        if base == base.parent:
            break
    return config_file


def _convert_toml_dict(d: dict[Any, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for k, v in d.items():
        key = k.decode() if isinstance(k, bytes) else str(k)
        if isinstance(v, dict):
            result[key] = _convert_toml_dict(v)
        elif isinstance(v, list):
            result[key] = [_convert_toml_dict(i) if isinstance(i, dict) else i for i in v]
        else:
            result[key] = v
    return result


def _merge_hunt_defaults(payload: dict[str, Any], hunt_defaults: Mapping[str, Any]) -> None:
    pinned = hunt_defaults.get("pinned", {}).get("defaults") if isinstance(hunt_defaults.get("pinned"), dict) else None
    if not isinstance(pinned, dict):
        pinned = hunt_defaults.get("pinned.defaults")
    if isinstance(pinned, dict):
        assets = payload.setdefault("assets", {})
        if not isinstance(assets, dict):
            assets = {}
            payload["assets"] = assets
        symbols = pinned.get("symbols") or []
        deep = pinned.get("analyst") if isinstance(pinned.get("analyst"), dict) else {}
        modes = pinned.get("modes") if isinstance(pinned.get("modes"), dict) else {}
        for sym in symbols:
            s = str(sym).strip().upper()
            if not s:
                continue
            block = assets.setdefault(s, {})
            if isinstance(block, dict):
                if s in modes:
                    block.setdefault("primary_timeframe", "15m")
                if deep.get(s):
                    block["analyst"] = True


def _normalize_bot_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = _convert_toml_dict(cast("dict[Any, Any]", payload))

    runtime_payload = normalized.get("runtime") if isinstance(normalized.get("runtime"), dict) else {}
    if not isinstance(runtime_payload, dict):
        runtime_payload = {}

    for field_name in ("strict_data_quality", "shortlist_unified_routing", "analysis_kline_intervals", "log_level", "telemetry_subdir"):
        if field_name in normalized and field_name not in runtime_payload:
            runtime_payload[field_name] = normalized[field_name]

    if runtime_payload:
        normalized["runtime"] = runtime_payload

    network_payload = normalized.get("network") if isinstance(normalized.get("network"), dict) else {}
    if not isinstance(network_payload, dict):
        network_payload = {}
    if network_payload:
        normalized["network"] = network_payload

    return normalized


def load_settings(config_path: str | Path = "config.toml") -> HuntSettings:
    config_file = Path(config_path)
    resolved = _resolve_config_source(config_file)
    parsed = _load_toml(resolved)
    bot_raw = parsed.get("bot") if isinstance(parsed.get("bot"), dict) else {}
    payload = _normalize_bot_payload(bot_raw)
    secrets = load_secrets()
    payload["tg_token"] = secrets.tg_token
    payload["target_chat_id"] = secrets.target_chat_id
    payload["operator_user_ids"] = list(secrets.operator_user_ids)
    payload["config_path"] = resolved
    payload.setdefault("data_dir", Path("data") / "bot")

    network_payload = payload.setdefault("network", {})
    if isinstance(network_payload, dict):
        env_proxy = str(os.getenv("BINANCE_PROXY_URL", "") or "").strip()
        if env_proxy:
            network_payload["proxy_url"] = env_proxy
        env_list = str(os.getenv("BINANCE_PROXY_URLS", "") or "").strip()
        if env_list:
            network_payload["proxy_urls"] = [x.strip() for x in env_list.split(",") if x.strip()]

    return HuntSettings.model_validate(payload)


def load_toml_defaults() -> dict[str, Any]:
    """Universal threshold defaults from hunt/config.defaults.toml (P11 merge)."""
    return load_config_defaults_toml()


_DEFAULTS_PATH = Path(__file__).resolve().parents[2] / "config.defaults.toml"


@lru_cache(maxsize=1)
def load_config_defaults_toml() -> dict[str, Any]:
    """Parse config.defaults.toml into param_store universal section keys."""
    if not _DEFAULTS_PATH.exists():
        return {}
    try:
        raw = _toml_lib.loads(_DEFAULTS_PATH.read_text(encoding="utf-8"))
    except (OSError, _toml_lib.TOMLDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}

    out: dict[str, Any] = {}
    scanner = raw.get("hunter")
    if isinstance(scanner, dict):
        out["scanner"] = {
            k: v
            for k, v in {
                "hot_range_pct": scanner.get("range_hot_pct"),
                "pump_extreme_pct": scanner.get("pump_extreme_pct"),
                "pos_near_high": scanner.get("pos_near_high"),
                "pos_near_low": scanner.get("pos_near_low"),
            }.items()
            if v is not None
        }

    confirm_root = raw.get("confirm") if isinstance(raw.get("confirm"), dict) else None
    if isinstance(confirm_root, dict):
        confirm_cfg: dict[str, Any] = {}
        for tf_key in ("entry_confirm_tf", "entry_confirm_tf_dump", "entry_confirm_tf_long"):
            tf_val = confirm_root.get(tf_key)
            if isinstance(tf_val, str) and tf_val.strip():
                confirm_cfg[tf_key] = tf_val.strip().lower()
        fast = confirm_root.get("dump_fast_confirm")
        if isinstance(fast, bool):
            confirm_cfg["dump_fast_confirm"] = fast
        if confirm_cfg:
            out["confirm"] = {**out.get("confirm", {}), **confirm_cfg}

    confirm_short = confirm_root.get("short") if isinstance(confirm_root, dict) else None
    if isinstance(confirm_short, dict):
        gates: dict[str, Any] = {}
        if confirm_short.get("min_score") is not None:
            gates["confirm_min_score"] = confirm_short["min_score"]
        if confirm_short.get("min_score_without_div") is not None:
            gates["confirm_min_score_no_div"] = confirm_short["min_score_without_div"]
        if confirm_short.get("forming_min_score") is not None:
            gates["forming_min_score"] = confirm_short["forming_min_score"]
        if gates:
            out["gates"] = gates

    levels = raw.get("levels", {}).get("adaptive") if isinstance(raw.get("levels"), dict) else None
    if isinstance(levels, dict):
        out["levels"] = {
            k: v
            for k, v in {
                "sl_max_pct_normal": levels.get("sl_max_pct_normal"),
                "sl_max_pct_hot": levels.get("sl_max_pct_hot"),
                "sl_max_pct_parabolic": levels.get("sl_max_pct_parabolic"),
                "hot_range_pct": levels.get("hot_range_pct"),
                "parabolic_range_pct": levels.get("parabolic_range_pct"),
                "parabolic_leg_gain_pct": levels.get("parabolic_leg_gain_pct"),
            }.items()
            if v is not None
        }

    lifecycle_sq = raw.get("lifecycle", {}).get("squeeze") if isinstance(raw.get("lifecycle"), dict) else None
    if isinstance(lifecycle_sq, dict):
        lc_block = dict(out.get("lifecycle") or {})
        for k, v in {
            "squeeze_bb_width_pctile_max": lifecycle_sq.get("bb_width_pctile_max"),
            "squeeze_donchian_width_pct_max": lifecycle_sq.get("donchian_width_pct_max"),
            "rsi_exhaustion_enter": lifecycle_sq.get("rsi_exhaustion_enter"),
            "rsi_exhaustion_exit": lifecycle_sq.get("rsi_exhaustion_exit"),
            "taker_buy_min": lifecycle_sq.get("taker_buy_min"),
            "taker_sell_max": lifecycle_sq.get("taker_sell_max"),
            "cascade_wick_ratio_min": lifecycle_sq.get("cascade_wick_ratio_min"),
        }.items():
            if v is not None:
                lc_block[k] = v
        if lc_block:
            out["lifecycle"] = lc_block

    for section in ("collect", "scoring", "tracker", "delivery", "intra_bar", "fusion"):
        block = raw.get(section)
        if isinstance(block, dict):
            out[section] = {k: v for k, v in block.items() if v is not None}

    return out


def universal_section_from_defaults(section: str) -> dict[str, Any]:
    block = load_config_defaults_toml().get(section)
    return dict(block) if isinstance(block, dict) else {}


# Watch-loop runtime constants (canonical — was runtime/settings.py)
COOLDOWN_MINUTES = 45
FORMING_MIN_SCORE = 45
MIN_RISK_REWARD = 1.0
HUNT_MIN_RISK_REWARD = 0.8
BOUNCE_MIN_RISK_REWARD = 0.5
SYMBOL_TICK_TIMEOUT_S = 180
SCAN_INTERVAL_S = 900
TICK_ROTATE_INTERVAL_S = 600
TICK_ROTATE_MIN_BYTES = 65_536

IGNITION_WINDOW_S = 300
IGNITION_MIN_PCT = 2.5
IGNITION_MIN_VOL_DELTA_USD = 250_000.0
IGNITION_MIN_QVOL_USD = 3_000_000.0
IGNITION_TTL_S = 7200.0
IGNITION_TELEGRAM_ENABLED = False

SQUEEZE_BB_PCTILE_MAX = 0.20
SQUEEZE_DONCHIAN_MAX_PCT = 8.0
SQUEEZE_MIN_VOL_24H_M = 5.0
SQUEEZE_COOLDOWN_MINUTES = 240


__all__ = [
    "AssetConfig",
    "BotSettings",
    "BOUNCE_MIN_RISK_REWARD",
    "COOLDOWN_MINUTES",
    "FilterConfig",
    "FORMING_MIN_SCORE",
    "HUNT_MIN_RISK_REWARD",
    "IGNITION_MIN_PCT",
    "IGNITION_MIN_QVOL_USD",
    "IGNITION_MIN_VOL_DELTA_USD",
    "IGNITION_TELEGRAM_ENABLED",
    "IGNITION_TTL_S",
    "IGNITION_WINDOW_S",
    "HuntSettings",
    "MIN_RISK_REWARD",
    "NetworkConfig",
    "REQUIRED_PINNED_SYMBOLS",
    "RuntimeConfig",
    "SCAN_INTERVAL_S",
    "SQUEEZE_BB_PCTILE_MAX",
    "SQUEEZE_COOLDOWN_MINUTES",
    "SQUEEZE_DONCHIAN_MAX_PCT",
    "SQUEEZE_MIN_VOL_24H_M",
    "SYMBOL_TICK_TIMEOUT_S",
    "SetupConfig",
    "TICK_ROTATE_INTERVAL_S",
    "TICK_ROTATE_MIN_BYTES",
    "load_config_defaults_toml",
    "load_settings",
    "load_toml_defaults",
    "universal_section_from_defaults",
]
