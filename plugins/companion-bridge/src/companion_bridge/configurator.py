from __future__ import annotations

import getpass
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

from .config import PLUGIN_ID, dispatch_paths, load_settings, plugin_paths
from .contracts import envelope, failure

InputFn = Callable[[str], str]
SecretFn = Callable[[str], str]
_SLACK_ID = re.compile(r"^[A-Z][A-Z0-9]{8,31}$")


def configure(
    request: Mapping[str, Any] | Any | None = None,
    *,
    input_fn: InputFn = input,
    secret_fn: SecretFn = getpass.getpass,
) -> dict[str, Any]:
    """Interactively write non-secret config and private Slack token material.

    Core invokes configurators with a trusted in-process context. Tests and
    source operators may also pass the bounded ``status`` request directly.
    Secret values are never accepted through JSON or returned.
    """
    context = None
    if request is not None and not isinstance(request, Mapping):
        if not all(hasattr(request, name) for name in ("paths", "prompt", "prompt_secret")):
            return failure("invalid", "invalid_input", "Configurator context is invalid.")
        context = request
        action = "configure"
        input_fn = context.prompt
        secret_fn = context.prompt_secret
    else:
        action = "configure" if request is None else request.get("action")
        if action not in {"configure", "status"} or (
            isinstance(request, Mapping) and set(request) != {"action"}
        ):
            return failure(
                str(action or "invalid"),
                "invalid_input",
                "Configurator accepts only {action: status|configure}.",
            )
    if action == "status":
        settings = load_settings(require_tokens=False)
        return envelope(
            ok=True,
            action="status",
            status="ready",
            data={
                "config_present": settings.config_path.is_file(),
                "slack_bot_token_present": settings.secrets.slack_bot_token_present,
                "slack_app_token_present": settings.secrets.slack_app_token_present,
                "allowed_channel_count": len(settings.config.slack.allowed_channels),
                "allowed_user_count": len(settings.config.slack.allowed_users),
            },
        )
    try:
        values = _prompt_values(input_fn, secret_fn)
        paths = plugin_paths(context.paths) if context is not None else dispatch_paths()
        _ensure_private_dir(paths.config_file.parent)
        _ensure_private_dir(paths.secret_file.parent)
        _atomic_private_write(
            paths.config_file,
            yaml.safe_dump(values["config"], sort_keys=True).encode("utf-8"),
            mode=0o600,
        )
        _atomic_private_write(
            paths.secret_file,
            _secret_text(values["bot_token"], values["app_token"]).encode("utf-8"),
            mode=0o600,
        )
    except (OSError, ValueError, UnicodeError):
        return failure(
            "configure",
            "configuration_failed",
            "Configuration could not be written securely.",
        )
    return envelope(
        ok=True,
        action="configure",
        status="configured",
        data={
            "config_path": paths.config_file.name,
            "secret_file": paths.secret_file.name,
            "slack_bot_token_present": bool(values["bot_token"]),
            "slack_app_token_present": bool(values["app_token"]),
        },
    )


def _prompt_values(input_fn: InputFn, secret_fn: SecretFn) -> dict[str, Any]:
    app_name = input_fn("Slack app name [DSP Companion]: ").strip() or "DSP Companion"
    channels = _csv(input_fn("Allowed Slack channel IDs (comma separated): "))
    users = _csv(input_fn("Allowed Slack user IDs (comma separated): "))
    teams = _csv(input_fn("Allowed Slack workspace IDs (optional): "))
    admin_channel = input_fn("Admin alert Slack channel ID: ").strip()
    bot_token = secret_fn("Slack bot token (hidden): ").strip()
    app_token = secret_fn("Slack app token (hidden): ").strip()
    if (
        not channels
        or not users
        or not admin_channel
        or not bot_token.startswith("xoxb-")
        or not app_token.startswith("xapp-")
        or any(not _SLACK_ID.fullmatch(value) for value in [*channels, *users, *teams, admin_channel])
    ):
        raise ValueError("required configuration is missing or invalid")
    return {
        "config": {
            "slack": {
                "app_name": app_name,
                "allowed_channels": channels,
                "allowed_users": users,
                "allowed_teams": teams,
                "admin_channel": admin_channel,
            }
        },
        "bot_token": bot_token,
        "app_token": app_token,
    }


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()][:256]


def _secret_text(bot_token: str, app_token: str) -> str:
    return f"SLACK_BOT_TOKEN={bot_token}\nSLACK_APP_TOKEN={app_token}\n"


def _ensure_private_dir(path: Path) -> None:
    missing: list[Path] = []
    current = path
    while not current.exists() and not current.is_symlink():
        missing.append(current)
        current = current.parent
    _check_dir(current, private=False)
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        _check_dir(directory, private=True)
    _check_dir(path, private=True)


def _check_dir(path: Path, *, private: bool) -> None:
    if path.is_symlink() or not path.is_dir():
        raise ValueError("private directory is unsafe")
    details = path.stat(follow_symlinks=False)
    if details.st_uid != os.geteuid() or (private and stat.S_IMODE(details.st_mode) != 0o700) or (not private and stat.S_IMODE(details.st_mode) & 0o022):
        raise ValueError("private directory ownership or mode is unsafe")


def _atomic_private_write(path: Path, content: bytes, *, mode: int) -> None:
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or path.stat().st_uid != os.geteuid() or stat.S_IMODE(path.stat().st_mode) != mode:
            raise ValueError("private file is unsafe")
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
