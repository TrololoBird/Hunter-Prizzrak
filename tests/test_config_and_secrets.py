from __future__ import annotations

from types import SimpleNamespace

from hunt_core.deliver.telegram import DisabledBroadcaster, build_message_broadcaster
from hunt_core.domain import config as domain_config
from hunt_core.secrets import load_secrets


def test_load_settings_prefers_user_config_as_single_source(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    defaults = root / "config.defaults.toml"
    defaults.write_text(
        """
[bot]
log_level = "WARNING"

[bot.network]
trust_env = false
""",
        encoding="utf-8",
    )
    user_config = root / "config.toml"
    user_config.write_text(
        """
[bot]
log_level = "DEBUG"

[bot.network]
trust_env = true
""",
        encoding="utf-8",
    )

    monkeypatch.chdir(root)

    settings = domain_config.load_settings(user_config)

    assert settings.runtime.log_level == "DEBUG"
    assert settings.network.trust_env is True


def test_build_message_broadcaster_disables_when_telegram_unconfigured():
    settings = SimpleNamespace(
        tg_token="",
        target_chat_id="",
        notifiers=SimpleNamespace(provider="telegram"),
    )

    broadcaster = build_message_broadcaster(settings)

    assert isinstance(broadcaster, DisabledBroadcaster)


def test_load_secrets_reads_dotenv_from_repo_ancestor(tmp_path, monkeypatch):
    repo_root = tmp_path / "workspace" / "repo"
    repo_root.mkdir(parents=True)
    env_dir = tmp_path / "workspace"
    (env_dir / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=ancestor-token\nTELEGRAM_CHAT_ID=456\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")

    secrets = load_secrets(repo_root)

    assert secrets.tg_token == "ancestor-token"
    assert secrets.target_chat_id == "456"
