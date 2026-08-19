from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

PLUGIN_ID = "companion-bridge"


class SlackConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app_name: str = Field(default="DSP Companion", max_length=120)
    allowed_teams: list[str] = Field(default_factory=list, max_length=64)
    allowed_users: list[str] = Field(default_factory=list, max_length=256)
    allowed_channels: list[str] = Field(default_factory=list, max_length=256)
    admin_users: list[str] = Field(default_factory=list, max_length=64)
    admin_channel: str = Field(default="", max_length=64)
    admin_alert_cooldown_seconds: int = Field(default=900, ge=1, le=86400)
    stream_buffer_chars: int = Field(default=256, ge=1, le=4096)
    max_slack_message_chars: int = Field(default=12000, ge=1, le=40000)
    allow_thread_replies_without_mention: bool = True
    enable_dms: bool = False


class AmazonConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    companion_url: str = Field(default="https://logistics.amazon.com/dspconsolev2", max_length=256)
    context_endpoint: str = Field(
        default="https://logistics.amazon.com/companion/platform/api/context", max_length=256
    )
    stream_endpoint: str = Field(
        default="https://logistics.amazon.com/companion/platform/api/conversations/stream", max_length=256
    )
    csrf_header: str = Field(default="anti-csrftoken-a2z", max_length=80)
    default_persona: str = Field(default="DSP", max_length=80)
    default_contract_types: list[str] = Field(default_factory=list, max_length=32)
    default_program_types: list[str] = Field(default_factory=list, max_length=32)
    request_timeout_seconds: float = Field(default=120.0, ge=1.0, le=600.0)
    auth_account_alias: str = Field(default="default", pattern=r"^[a-z][a-z0-9-]{0,62}$")


class SessionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ttl_hours: int = Field(default=72, ge=1, le=720)
    cleanup_interval_seconds: int = Field(default=1800, ge=30, le=86400)


class LimitsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_prompt_chars: int = Field(default=5000, ge=1, le=20000)
    max_concurrent_streams: int = Field(default=2, ge=1, le=32)
    per_user_concurrent_streams: int = Field(default=1, ge=1, le=8)
    acquire_timeout_seconds: float = Field(default=1.0, ge=0.0, le=60.0)


class LoggingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: str = Field(default="INFO", max_length=20)
    log_prompts: bool = False
    log_responses: bool = False


class DriverNamesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    id_regex: str = Field(default=r"\bA[A-Z0-9]{10,24}\b", max_length=128)
    fallback_to_id: bool = True


class BridgeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slack: SlackConfig = Field(default_factory=SlackConfig)
    amazon: AmazonConfig = Field(default_factory=AmazonConfig)
    sessions: SessionConfig = Field(default_factory=SessionConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    driver_names: DriverNamesConfig = Field(default_factory=DriverNamesConfig)


class SecretPresence(BaseModel):
    slack_bot_token_present: bool = False
    slack_app_token_present: bool = False


class LoadedSettings(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    config: BridgeConfig
    config_path: Path
    secret_path: Path
    database_path: Path
    secrets: SecretPresence
    missing_required_secrets: list[str] = Field(default_factory=list)
    config_errors: list[str] = Field(default_factory=list)
    security_errors: list[str] = Field(default_factory=list)
    slack_bot_token: str | None = Field(default=None, exclude=True, repr=False)
    slack_app_token: str | None = Field(default=None, exclude=True, repr=False)


@dataclass(frozen=True)
class PluginPaths:
    config_file: Path
    secret_file: Path
    database_file: Path


def plugin_paths(core_paths: Any) -> PluginPaths:
    """Resolve owner-private paths from an already validated Core path object."""
    return PluginPaths(
        config_file=core_paths.owner_root("config", PLUGIN_ID) / "config.yaml",
        secret_file=core_paths.owner_root("secrets", PLUGIN_ID) / "slack.env",
        database_file=core_paths.owner_root("state", PLUGIN_ID) / "threads.sqlite3",
    )


def dispatch_paths(environ: Mapping[str, str] | None = None) -> PluginPaths:
    """Derive all private paths through Core's DispatchPaths contract."""
    from paths import DispatchPaths

    return plugin_paths(DispatchPaths.from_environment(environ))


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load_yaml(path: Path) -> tuple[dict[str, Any], list[str]]:
    raw, read_error = _read_private_file(path, maximum=64 * 1024)
    if read_error:
        return {}, ["configuration file is unsafe or unreadable"]
    if raw is None:
        return {}, []
    try:
        loaded = yaml.safe_load(raw.decode("utf-8", errors="strict"))
    except (UnicodeError, yaml.YAMLError):
        return {}, ["configuration file could not be parsed"]
    if loaded is None:
        return {}, []
    if not isinstance(loaded, dict):
        return {}, ["configuration root must be an object"]
    return loaded, []


def parse_secret_file(path: Path) -> dict[str, str]:
    values, _ = _parse_secret_file(path)
    return values


def _parse_secret_file(path: Path) -> tuple[dict[str, str], str | None]:
    values: dict[str, str] = {}
    raw, read_error = _read_private_file(path, maximum=16 * 1024)
    if read_error:
        return {}, "Slack secret file is unsafe or unreadable"
    if raw is None:
        return {}, None
    try:
        lines = raw.decode("utf-8", errors="strict").splitlines()
    except UnicodeError:
        return {}, "Slack secret file could not be decoded"
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key in {"SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"} and value:
            values[key] = value
    return values, None


def _read_private_file(path: Path, *, maximum: int) -> tuple[bytes | None, str | None]:
    if not path.exists() and not path.is_symlink():
        return None, None
    try:
        parent = path.parent.lstat()
        details = path.lstat()
    except OSError:
        return None, "unreadable"
    if (
        path.parent.is_symlink()
        or not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.geteuid()
        or stat.S_IMODE(parent.st_mode) & 0o077
        or path.is_symlink()
        or not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.geteuid()
        or details.st_nlink != 1
        or stat.S_IMODE(details.st_mode) & 0o077
        or details.st_size > maximum
    ):
        return None, "unsafe"
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (details.st_dev, details.st_ino):
                return None, "changed"
            raw = os.read(descriptor, maximum + 1)
        finally:
            os.close(descriptor)
    except OSError:
        return None, "unreadable"
    return (raw, None) if len(raw) <= maximum else (None, "oversized")


def slack_access_security_errors(config: SlackConfig) -> list[str]:
    errors: list[str] = []
    if not config.allowed_channels:
        errors.append("slack.allowed_channels must list approved channels")
    if not config.allowed_users:
        errors.append("slack.allowed_users must list approved users")
    if not config.admin_channel and not config.admin_users:
        errors.append("slack must configure an admin channel or admin user")
    return errors


def load_settings(
    config_path: Path | str | None = None,
    secret_path: Path | str | None = None,
    database_path: Path | str | None = None,
    *,
    require_tokens: bool = True,
    environ: Mapping[str, str] | None = None,
) -> LoadedSettings:
    errors: list[str] = []
    if config_path is not None:
        config_value = Path(config_path)
        paths = PluginPaths(
            config_value,
            Path(secret_path) if secret_path is not None else config_value.with_name(".env"),
            Path(database_path) if database_path is not None else config_value.with_name("threads.sqlite3"),
        )
    else:
        try:
            paths = dispatch_paths(environ)
        except Exception:
            unavailable = Path("/nonexistent/dispatch-companion-bridge")
            paths = PluginPaths(unavailable / "config.yaml", unavailable / "slack.env", unavailable / "threads.sqlite3")
            errors.append("Dispatch private paths are not configured")
    config_file = paths.config_file
    secret_file = paths.secret_file
    database_file = paths.database_file

    yaml_data, yaml_errors = _load_yaml(config_file)
    errors.extend(yaml_errors)
    try:
        config = BridgeConfig.model_validate(_deep_merge(BridgeConfig().model_dump(), yaml_data))
    except ValidationError:
        config = BridgeConfig()
        errors.append("configuration values are invalid")

    file_values, secret_error = _parse_secret_file(secret_file)
    if secret_error:
        errors.append(secret_error)
    bot_token = file_values.get("SLACK_BOT_TOKEN")
    app_token = file_values.get("SLACK_APP_TOKEN")
    missing = []
    if require_tokens:
        if not bot_token:
            missing.append("SLACK_BOT_TOKEN")
        if not app_token:
            missing.append("SLACK_APP_TOKEN")
    return LoadedSettings(
        config=config,
        config_path=config_file,
        secret_path=secret_file,
        database_path=database_file,
        secrets=SecretPresence(
            slack_bot_token_present=bool(bot_token),
            slack_app_token_present=bool(app_token),
        ),
        missing_required_secrets=missing,
        config_errors=errors,
        security_errors=slack_access_security_errors(config.slack),
        slack_bot_token=bot_token,
        slack_app_token=app_token,
    )
