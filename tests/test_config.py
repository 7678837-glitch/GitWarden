from pathlib import Path

import pytest

from gitwarden.config import ConfigError, load_config


def _env(tmp_path: Path, config_path: Path, **extra: str) -> dict[str, str]:
    return {
        "GW_DATA_DIR": str(tmp_path / "data"),
        "GW_CONFIG": str(config_path),
        "GW_ADMIN_PASSWORD": "test-password",
        **extra,
    }


def test_scheduler_and_log_settings_load_from_toml(tmp_path: Path) -> None:
    config_path = tmp_path / "gitwarden.toml"
    config_path.write_text(
        """
[server]
port = 9090
[scheduler]
tick_seconds = 0.25
workers = 3
min_sync_minutes = 6
min_discovery_minutes = 16
stall_minutes = 11
[logs]
retention_days = 30
"""
    )

    config = load_config(_env(tmp_path, config_path))

    assert config.port == 9090
    assert config.tick_seconds == 0.25
    assert config.workers == 3
    assert config.min_sync_minutes == 6
    assert config.min_discovery_minutes == 16
    assert config.stall_minutes == 11
    assert config.log_retention_days == 30


def test_environment_overrides_scheduler_and_log_settings(tmp_path: Path) -> None:
    config_path = tmp_path / "gitwarden.toml"
    config_path.write_text(
        """
[scheduler]
workers = 3
min_sync_minutes = 6
min_discovery_minutes = 16
stall_minutes = 11
[logs]
retention_days = 30
"""
    )

    config = load_config(
        _env(
            tmp_path,
            config_path,
            GW_WORKERS="4",
            GW_MIN_SYNC_MINUTES="7",
            GW_MIN_DISCOVERY_MINUTES="17",
            GW_STALL_MINUTES="12",
            GW_LOG_RETENTION_DAYS="45",
        )
    )

    assert config.workers == 4
    assert config.min_sync_minutes == 7
    assert config.min_discovery_minutes == 17
    assert config.stall_minutes == 12
    assert config.log_retention_days == 45


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("GW_WORKERS", "0"),
        ("GW_WORKERS", "5"),
        ("GW_MIN_SYNC_MINUTES", "0"),
        ("GW_MIN_DISCOVERY_MINUTES", "0"),
        ("GW_STALL_MINUTES", "0"),
        ("GW_LOG_RETENTION_DAYS", "0"),
    ],
)
def test_invalid_scheduler_or_log_settings_are_rejected(
    tmp_path: Path, key: str, value: str
) -> None:
    config_path = tmp_path / "gitwarden.toml"

    with pytest.raises(ConfigError):
        load_config(_env(tmp_path, config_path, **{key: value}))
