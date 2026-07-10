"""Environment-backed runtime secrets.

Centralizes secret loading so config/model code doesn't duplicate env parsing.
"""
from __future__ import annotations



import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Secrets:
    tg_token: str
    target_chat_id: str
    lab_chat_id: str = ""
    operator_user_ids: tuple[int, ...] = ()


def parse_operator_user_ids(raw: str) -> tuple[int, ...]:
    """Parse comma/space-separated Telegram user ids for operator console."""
    ids: list[int] = []
    for chunk in str(raw or "").replace(";", ",").split(","):
        piece = chunk.strip()
        if not piece:
            continue
        try:
            ids.append(int(piece))
        except ValueError:
            continue
    return tuple(sorted(set(ids)))


def _first_configured_env(*names: str) -> str:
    # fix-20260604: skip empty canonical keys so legacy TG_TOKEN / TARGET_CHAT_ID still work
    for name in names:
        if name not in os.environ:
            continue
        value = os.environ[name].strip()
        if value:
            return value
    return ""


def _clear_blank_env_values(*names: str) -> None:
    for name in names:
        value = os.environ.get(name)
        if isinstance(value, str) and not value.strip():
            os.environ.pop(name, None)


def load_secrets(base_dir: str | os.PathLike[str] | None = None) -> Secrets:
    # Canonical env keys: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    # Legacy aliases TG_TOKEN / TARGET_CHAT_ID are deprecated - do not introduce new references.
    cwd = Path.cwd().resolve()
    base_path = Path(base_dir).resolve() if base_dir is not None else cwd
    if base_path.is_file():
        base_path = base_path.parent

    search_roots: list[Path] = []
    if base_path.exists():
        search_roots.extend([base_path, *base_path.parents])
    if cwd.exists():
        search_roots.extend([cwd, *cwd.parents])

    seen: set[Path] = set()
    dotenv_path: Path | None = None
    for directory in search_roots:
        if not directory.exists() or directory in seen:
            continue
        seen.add(directory)
        candidate = directory / ".env"
        if candidate.exists():
            dotenv_path = candidate
            break

    env_names = (
        "TELEGRAM_BOT_TOKEN",
        "TG_TOKEN",
        "TELEGRAM_CHAT_ID",
        "TARGET_CHAT_ID",
        "TELEGRAM_LAB_CHAT_ID",
        "HUNT_LAB_CHAT_ID",
        "TELEGRAM_OPERATOR_USER_IDS",
        "OPERATOR_USER_IDS",
    )
    if dotenv_path is not None:
        _clear_blank_env_values(*env_names)
        load_dotenv(dotenv_path, override=False)
    else:
        _clear_blank_env_values(*env_names)
        load_dotenv(override=False)

    tg_token = _first_configured_env("TELEGRAM_BOT_TOKEN", "TG_TOKEN")
    target_chat_id = _first_configured_env("TELEGRAM_CHAT_ID", "TARGET_CHAT_ID")
    lab_chat_id = _first_configured_env("TELEGRAM_LAB_CHAT_ID", "HUNT_LAB_CHAT_ID")
    operator_raw = _first_configured_env("TELEGRAM_OPERATOR_USER_IDS", "OPERATOR_USER_IDS")
    return Secrets(
        tg_token=tg_token,
        target_chat_id=target_chat_id,
        lab_chat_id=lab_chat_id,
        operator_user_ids=parse_operator_user_ids(operator_raw),
    )
