"""配置加载：环境变量 > TOML 文件 > 默认值。"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    """配置错误，消息直接显示给管理员。"""


@dataclass(frozen=True)
class Config:
    data_dir: Path
    port: int = 8080
    tick_seconds: float = 30
    workers: int = 2
    min_sync_minutes: int = 5
    min_discovery_minutes: int = 15
    stall_minutes: float = 10
    log_retention_days: int = 90
    admin_password: str = ""


def _read_toml(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"配置文件格式错误 {path}: {exc}") from exc


def _number(env: Mapping[str, str], key: str, fallback, cast):
    raw = env.get(key)
    if raw is None or raw == "":
        return fallback
    try:
        return cast(raw)
    except ValueError:
        raise ConfigError(f"环境变量 {key} 的值无效: {raw!r}") from None


def _validate(config: Config) -> Config:
    if not 1 <= config.workers <= 4:
        raise ConfigError("scheduler workers 必须在 1 到 4 之间")
    if config.min_sync_minutes < 1:
        raise ConfigError("同步间隔下限必须至少为 1 分钟")
    if config.min_discovery_minutes < 1:
        raise ConfigError("发现间隔下限必须至少为 1 分钟")
    if config.stall_minutes <= 0:
        raise ConfigError("卡死检测间隔必须大于 0 分钟")
    if config.log_retention_days < 1:
        raise ConfigError("日志保留天数必须至少为 1 天")
    if config.tick_seconds <= 0:
        raise ConfigError("调度 tick 必须大于 0 秒")
    return config


def load_config(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    data_dir = Path(env.get("GW_DATA_DIR") or "./data")
    config_path = Path(env.get("GW_CONFIG") or data_dir / "config" / "gitwarden.toml")
    toml = _read_toml(config_path)
    server = toml.get("server", {})
    scheduler = toml.get("scheduler", {})
    logs = toml.get("logs", {})

    port = _number(env, "GW_PORT", server.get("port", 8080), int)
    tick = _number(env, "GW_TICK_SECONDS", scheduler.get("tick_seconds", 30), float)
    workers = _number(env, "GW_WORKERS", scheduler.get("workers", 2), int)
    min_sync = _number(
        env,
        "GW_MIN_SYNC_MINUTES",
        scheduler.get("min_sync_minutes", 5),
        int,
    )
    min_discovery = _number(
        env,
        "GW_MIN_DISCOVERY_MINUTES",
        scheduler.get("min_discovery_minutes", 15),
        int,
    )
    stall = _number(env, "GW_STALL_MINUTES", scheduler.get("stall_minutes", 10), float)
    retention = _number(
        env,
        "GW_LOG_RETENTION_DAYS",
        logs.get("retention_days", 90),
        int,
    )
    raw_password = env.get("GW_ADMIN_PASSWORD", "")
    if not isinstance(raw_password, str):
        raise ConfigError("未设置管理员密码：请在环境变量 GW_ADMIN_PASSWORD 中设置后再启动")
    password = raw_password.strip()
    if not password:
        raise ConfigError("未设置管理员密码：请在环境变量 GW_ADMIN_PASSWORD 中设置后再启动")
    return _validate(
        Config(
            data_dir=data_dir,
            port=port,
            tick_seconds=tick,
            workers=workers,
            min_sync_minutes=min_sync,
            min_discovery_minutes=min_discovery,
            stall_minutes=stall,
            log_retention_days=retention,
            admin_password=password,
        )
    )
