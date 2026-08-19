"""Systemd user-service publication."""
from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Iterable, Mapping

from .layout import (
    InstallLayout,
    InstallerError,
    assert_user_owned_directory,
    atomic_json,
    ensure_private_directory,
    read_json,
)

RunCommand = Callable[[Sequence[str], Path | None], subprocess.CompletedProcess[str]]

_PLUGIN_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


def _run(command: Sequence[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, check=False, capture_output=True, text=True)


def _quote_systemd(value: str) -> str:
    return '"' + value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"') + '"'


def _plugin_service_id(plugin_id: str) -> str:
    if not isinstance(plugin_id, str) or not _PLUGIN_ID.fullmatch(plugin_id):
        raise InstallerError("plugin_id_invalid", "plugin service ID is invalid")
    return plugin_id


def plugin_service_name(plugin_id: str) -> str:
    return f"dispatch-plugin-{_plugin_service_id(plugin_id)}.service"


def plugin_service_path(layout: InstallLayout, plugin_id: str) -> Path:
    return layout.service_directory / plugin_service_name(plugin_id)


def plugin_service_receipt_path(layout: InstallLayout, plugin_id: str) -> Path:
    return layout.state / "plugins" / "services" / f"{_plugin_service_id(plugin_id)}.json"


def plugin_service_unit(layout: InstallLayout, plugin_id: str) -> bytes:
    """Return a secret-free unit for a selected long-running plugin."""

    plugin_id = _plugin_service_id(plugin_id)
    return (
        "[Unit]\n"
        f"Description=Dispatch plugin {plugin_id}\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        "UMask=0077\n"
        f"ExecStart={_quote_systemd(str(layout.command_path))} plugin serve {_quote_systemd(plugin_id)}\n"
        f"Environment=DISPATCH_HOME={_quote_systemd(str(layout.dispatch_home))}\n"
        f"Environment=DISPATCH_CONFIG_ROOT={_quote_systemd(str(layout.config))}\n"
        f"Environment=DISPATCH_SECRETS_ROOT={_quote_systemd(str(layout.secrets))}\n"
        f"Environment=DISPATCH_DATA_ROOT={_quote_systemd(str(layout.data))}\n"
        f"Environment=DISPATCH_STATE_ROOT={_quote_systemd(str(layout.state))}\n"
        f"Environment=DISPATCH_CACHE_ROOT={_quote_systemd(str(layout.cache))}\n"
        f"Environment=DISPATCH_LOGS_ROOT={_quote_systemd(str(layout.logs))}\n"
        f"Environment=DISPATCH_RUNTIME_ROOT={_quote_systemd(str(layout.run))}\n"
        f"Environment=PLAYWRIGHT_BROWSERS_PATH={_quote_systemd(str(layout.browser_cache))}\n"
        "Restart=on-failure\n"
        "RestartSec=2\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    ).encode("utf-8")


def _plugin_receipt(layout: InstallLayout, plugin_id: str, content: bytes, status: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "plugin_id": _plugin_service_id(plugin_id),
        "unit": str(plugin_service_path(layout, plugin_id)),
        "unit_sha256": hashlib.sha256(content).hexdigest(),
        "service": plugin_service_name(plugin_id),
        "status": status,
        "contains_secrets": False,
    }


def _read_plugin_receipt(layout: InstallLayout, plugin_id: str) -> dict[str, object] | None:
    try:
        payload = read_json(plugin_service_receipt_path(layout, plugin_id), maximum=16 * 1024)
    except InstallerError:
        return None
    expected = {"schema_version", "plugin_id", "unit", "unit_sha256", "service", "status", "contains_secrets"}
    if (
        set(payload) != expected
        or payload.get("schema_version") != 1
        or payload.get("plugin_id") != plugin_id
        or payload.get("unit") != str(plugin_service_path(layout, plugin_id))
        or payload.get("service") != plugin_service_name(plugin_id)
        or payload.get("status") not in {"prepared", "enabled", "disabled"}
        or payload.get("contains_secrets") is not False
        or not isinstance(payload.get("unit_sha256"), str)
    ):
        return None
    return payload


def _plugin_receipt_is_owned(layout: InstallLayout, plugin_id: str) -> bool:
    path = plugin_service_receipt_path(layout, plugin_id)
    try:
        details = path.lstat()
    except OSError:
        return False
    return (
        not path.is_symlink()
        and stat.S_ISREG(details.st_mode)
        and details.st_uid == os.geteuid()
        and details.st_nlink == 1
        and stat.S_IMODE(details.st_mode) == 0o600
        and _read_plugin_receipt(layout, plugin_id) is not None
    )


def plugin_service_unit_is_owned(layout: InstallLayout, plugin_id: str) -> bool:
    try:
        plugin_id = _plugin_service_id(plugin_id)
        assert_user_owned_directory(layout.service_directory, "service directory")
        path = plugin_service_path(layout, plugin_id)
        if path.is_symlink() or not path.is_file():
            return False
        details = path.stat(follow_symlinks=False)
        if details.st_uid != os.geteuid() or details.st_nlink != 1 or stat.S_IMODE(details.st_mode) != 0o600:
            return False
        content = path.read_bytes()
        receipt = _read_plugin_receipt(layout, plugin_id)
        return (
            len(content) <= 64 * 1024
            and content == plugin_service_unit(layout, plugin_id)
            and receipt is not None
            and _plugin_receipt_is_owned(layout, plugin_id)
            and receipt.get("unit_sha256") == hashlib.sha256(content).hexdigest()
        )
    except (InstallerError, OSError):
        return False


def _write_plugin_service(layout: InstallLayout, plugin_id: str, *, status: str) -> dict[str, object]:
    plugin_id = _plugin_service_id(plugin_id)
    ensure_private_directory(layout.service_directory, "service directory")
    receipt_path = plugin_service_receipt_path(layout, plugin_id)
    ensure_private_directory(receipt_path.parent, "plugin service state")
    path = plugin_service_path(layout, plugin_id)
    content = plugin_service_unit(layout, plugin_id)
    if path.exists() or path.is_symlink():
        if not plugin_service_unit_is_owned(layout, plugin_id):
            raise InstallerError("plugin_service_conflict", "existing plugin service unit is not Dispatch-owned")
    _atomic_bytes(path, content)
    receipt = _plugin_receipt(layout, plugin_id, content, status)
    atomic_json(receipt_path, receipt)
    return {"status": status, "plugin_id": plugin_id, "unit": str(path), "service": plugin_service_name(plugin_id)}


def prepare_plugin_service(layout: InstallLayout, plugin_id: str) -> dict[str, object]:
    """Publish a disabled, receipt-owned service projection without starting it."""

    plugin_id = _plugin_service_id(plugin_id)
    previous = _read_plugin_receipt(layout, plugin_id)
    status = "prepared"
    if previous is not None and previous.get("status") == "enabled" and plugin_service_unit_is_owned(layout, plugin_id):
        status = "enabled"
    return _write_plugin_service(layout, plugin_id, status=status)


def _set_plugin_receipt_status(layout: InstallLayout, plugin_id: str, status: str) -> None:
    path = plugin_service_receipt_path(layout, plugin_id)
    content = plugin_service_path(layout, plugin_id).read_bytes()
    atomic_json(path, _plugin_receipt(layout, plugin_id, content, status))


def enable_plugin_service(
    layout: InstallLayout,
    plugin_id: str,
    *,
    run: RunCommand = _run,
) -> dict[str, object]:
    plugin_id = _plugin_service_id(plugin_id)
    unit_path = plugin_service_path(layout, plugin_id)
    receipt_path = plugin_service_receipt_path(layout, plugin_id)
    unit_before: bytes | None = None
    receipt_before: bytes | None = None
    active_before = False
    enabled_before = False
    systemd_touched = False
    if unit_path.exists() or unit_path.is_symlink():
        if not plugin_service_unit_is_owned(layout, plugin_id):
            raise InstallerError("plugin_service_unsafe", "plugin service unit is not Dispatch-owned")
        unit_before = unit_path.read_bytes()
        receipt_before = receipt_path.read_bytes()
        service_name = plugin_service_name(plugin_id)
        active_before = run(("systemctl", "--user", "is-active", "--quiet", service_name), None).returncode == 0
        enabled_before = run(("systemctl", "--user", "is-enabled", "--quiet", service_name), None).returncode == 0
    elif receipt_path.exists() or receipt_path.is_symlink():
        raise InstallerError("plugin_service_unsafe", "plugin service receipt has no owned unit")

    try:
        prepared = prepare_plugin_service(layout, plugin_id)
        service = str(prepared["service"])
        preflight = run((str(layout.command_path), "plugin", "health", plugin_id), None)
        if preflight.returncode != 0:
            raise InstallerError(
                "plugin_service_not_ready",
                "plugin configuration health must pass before its service can be enabled",
            )
        for command in (
            ("systemctl", "--user", "daemon-reload"),
            ("systemctl", "--user", "enable", "--now", service),
            ("systemctl", "--user", "restart", service),
        ):
            if run(command, None).returncode != 0:
                raise InstallerError("plugin_service_activation_failed", "plugin service could not be enabled and restarted")
            systemd_touched = True
        current = status_plugin_service(layout, plugin_id, run=run)
        if current.get("active") is not True or current.get("enabled") is not True:
            raise InstallerError("plugin_service_activation_failed", "plugin service did not remain active and enabled")
        _set_plugin_receipt_status(layout, plugin_id, "enabled")
        prepared["status"] = "enabled"
        return prepared
    except BaseException as activation_error:
        rollback_failure: BaseException | None = None

        def attempt(action: Callable[[], object]) -> None:
            nonlocal rollback_failure
            try:
                result = action()
                if isinstance(result, subprocess.CompletedProcess) and result.returncode != 0:
                    raise InstallerError("plugin_service_rollback_failed", "plugin service rollback command failed")
            except BaseException as exc:
                rollback_failure = rollback_failure or exc

        if systemd_touched:
            attempt(lambda: run(("systemctl", "--user", "disable", "--now", plugin_service_name(plugin_id)), None))
        if unit_before is None:
            attempt(lambda: unit_path.unlink(missing_ok=True))
            attempt(lambda: receipt_path.unlink(missing_ok=True))
        else:
            attempt(lambda: _atomic_bytes(unit_path, unit_before))
            if receipt_before is not None:
                attempt(lambda: _atomic_bytes(receipt_path, receipt_before))
        if systemd_touched:
            attempt(lambda: run(("systemctl", "--user", "daemon-reload"), None))
        if enabled_before:
            attempt(lambda: run(("systemctl", "--user", "enable", "--now", plugin_service_name(plugin_id)), None))
        elif active_before:
            attempt(lambda: run(("systemctl", "--user", "start", plugin_service_name(plugin_id)), None))
        if rollback_failure is not None:
            raise InstallerError(
                "plugin_service_activation_rollback_failed",
                "plugin service activation failed and its previous state could not be restored",
            ) from activation_error
        raise


def disable_plugin_service(
    layout: InstallLayout,
    plugin_id: str,
    *,
    run: RunCommand = _run,
) -> dict[str, object]:
    plugin_id = _plugin_service_id(plugin_id)
    path = plugin_service_path(layout, plugin_id)
    if not path.exists() and not path.is_symlink():
        return {"status": "missing", "plugin_id": plugin_id, "unit": str(path), "service": plugin_service_name(plugin_id)}
    if not plugin_service_unit_is_owned(layout, plugin_id):
        raise InstallerError("plugin_service_unsafe", "plugin service unit is not Dispatch-owned")
    service = plugin_service_name(plugin_id)
    if run(("systemctl", "--user", "disable", "--now", service), None).returncode != 0:
        raise InstallerError("plugin_service_stop_failed", "plugin service could not be disabled")
    _set_plugin_receipt_status(layout, plugin_id, "disabled")
    return {"status": "disabled", "plugin_id": plugin_id, "unit": str(path), "service": service}


def status_plugin_service(
    layout: InstallLayout,
    plugin_id: str,
    *,
    run: RunCommand = _run,
) -> dict[str, object]:
    plugin_id = _plugin_service_id(plugin_id)
    path = plugin_service_path(layout, plugin_id)
    if not path.exists() and not path.is_symlink():
        receipt = plugin_service_receipt_path(layout, plugin_id)
        return {
            "status": "unsafe" if receipt.exists() or receipt.is_symlink() else "missing",
            "plugin_id": plugin_id,
            "unit": str(path),
            "service": plugin_service_name(plugin_id),
        }
    if not plugin_service_unit_is_owned(layout, plugin_id):
        return {"status": "unsafe", "plugin_id": plugin_id, "unit": str(path), "service": plugin_service_name(plugin_id)}
    service = plugin_service_name(plugin_id)
    active = run(("systemctl", "--user", "is-active", "--quiet", service), None).returncode == 0
    enabled = run(("systemctl", "--user", "is-enabled", "--quiet", service), None).returncode == 0
    receipt = _read_plugin_receipt(layout, plugin_id)
    return {
        "status": "ready" if active and enabled else ("prepared" if not active and not enabled else "incomplete"),
        "plugin_id": plugin_id,
        "unit": str(path),
        "service": service,
        "active": active,
        "enabled": enabled,
        "receipt_status": receipt.get("status") if receipt else None,
    }


def remove_plugin_service(
    layout: InstallLayout,
    plugin_id: str,
    *,
    run: RunCommand = _run,
) -> None:
    plugin_id = _plugin_service_id(plugin_id)
    path = plugin_service_path(layout, plugin_id)
    receipt_path = plugin_service_receipt_path(layout, plugin_id)
    if not path.exists() and not path.is_symlink():
        if receipt_path.exists() or receipt_path.is_symlink():
            if not _plugin_receipt_is_owned(layout, plugin_id):
                raise InstallerError("plugin_service_unsafe", "plugin service receipt is unsafe")
            receipt_path.unlink()
        return
    if not plugin_service_unit_is_owned(layout, plugin_id):
        raise InstallerError("plugin_service_unsafe", "plugin service unit is not Dispatch-owned")
    service = plugin_service_name(plugin_id)
    previous_receipt = _read_plugin_receipt(layout, plugin_id)
    if run(("systemctl", "--user", "disable", "--now", service), None).returncode != 0:
        raise InstallerError("plugin_service_stop_failed", "plugin service could not be stopped")
    content = path.read_bytes()
    path.unlink()
    if run(("systemctl", "--user", "daemon-reload"), None).returncode != 0:
        try:
            _atomic_bytes(path, content)
            if previous_receipt is not None:
                atomic_json(receipt_path, previous_receipt)
            reload_result = run(("systemctl", "--user", "daemon-reload"), None)
            if reload_result.returncode == 0 and previous_receipt is not None and previous_receipt.get("status") == "enabled":
                run(("systemctl", "--user", "enable", "--now", service), None)
        finally:
            raise InstallerError("plugin_service_reload_failed", "systemd user manager could not reload")
    receipt_path.unlink(missing_ok=True)


def inspect_plugin_services(
    layout: InstallLayout,
    plugin_ids: Iterable[str] = (),
    *,
    run: RunCommand = _run,
) -> dict[str, object]:
    try:
        ids = plugin_service_ids(layout, plugin_ids)
    except InstallerError as exc:
        return {"status": "unsafe", "services": {}, "error": str(exc)[:256]}
    services: dict[str, object] = {}
    for plugin_id in sorted(ids):
        try:
            services[plugin_id] = status_plugin_service(layout, plugin_id, run=run)
        except (InstallerError, OSError) as exc:
            services[plugin_id] = {"status": "unsafe", "plugin_id": plugin_id, "error": str(exc)[:256]}
    healthy = all(
        isinstance(item, dict) and item.get("status") in {"ready", "prepared", "missing"}
        for item in services.values()
    )
    return {"status": "ready" if healthy else "unsafe", "services": services}


def plugin_service_ids(layout: InstallLayout, plugin_ids: Iterable[str] = ()) -> set[str]:
    ids = {_plugin_service_id(plugin_id) for plugin_id in plugin_ids}
    receipt_root = layout.state / "plugins" / "services"
    if receipt_root.exists() or receipt_root.is_symlink():
        if receipt_root.is_symlink() or not receipt_root.is_dir():
            raise InstallerError("plugin_service_unsafe", "plugin service receipt root is unsafe")
        for receipt in receipt_root.glob("*.json"):
            if receipt.is_symlink() or not receipt.is_file():
                raise InstallerError("plugin_service_unsafe", "plugin service receipt is unsafe")
            ids.add(_plugin_service_id(receipt.stem))
    if layout.service_directory.exists() or layout.service_directory.is_symlink():
        assert_user_owned_directory(layout.service_directory, "service directory")
        for unit in layout.service_directory.glob("dispatch-plugin-*.service"):
            name = unit.name.removeprefix("dispatch-plugin-").removesuffix(".service")
            ids.add(_plugin_service_id(name))
    return ids


def remove_plugin_services(
    layout: InstallLayout,
    plugin_ids: Iterable[str],
    *,
    run: RunCommand = _run,
) -> None:
    for plugin_id in sorted(set(plugin_ids)):
        remove_plugin_service(layout, plugin_id, run=run)


def prepare_plugin_services(layout: InstallLayout, plugin_ids: Iterable[str]) -> list[dict[str, object]]:
    return [prepare_plugin_service(layout, plugin_id) for plugin_id in sorted(set(plugin_ids))]


def enable_plugin_services(
    layout: InstallLayout,
    plugin_ids: Iterable[str],
    *,
    run: RunCommand = _run,
) -> list[dict[str, object]]:
    return [enable_plugin_service(layout, plugin_id, run=run) for plugin_id in sorted(set(plugin_ids))]


def disable_plugin_services(
    layout: InstallLayout,
    plugin_ids: Iterable[str],
    *,
    run: RunCommand = _run,
) -> list[dict[str, object]]:
    return [disable_plugin_service(layout, plugin_id, run=run) for plugin_id in sorted(set(plugin_ids))]


def status_plugin_services(
    layout: InstallLayout,
    plugin_ids: Iterable[str],
    *,
    run: RunCommand = _run,
) -> dict[str, dict[str, object]]:
    return {
        plugin_id: status_plugin_service(layout, plugin_id, run=run)
        for plugin_id in sorted(set(plugin_ids))
    }


def stop_plugin_services_for_activation(
    layout: InstallLayout,
    plugin_ids: Iterable[str],
    *,
    run: RunCommand = _run,
) -> list[dict[str, object]]:
    """Stop every active/enabled owned plugin service and retain its exact state."""
    stopped: list[dict[str, object]] = []
    for plugin_id in sorted(plugin_service_ids(layout, plugin_ids)):
        status = status_plugin_service(layout, plugin_id, run=run)
        if status.get("status") == "unsafe":
            raise InstallerError("plugin_service_unsafe", "plugin service unit is not Dispatch-owned")
        active = status.get("active") is True
        enabled = status.get("enabled") is True
        if not active and not enabled:
            continue
        service = plugin_service_name(plugin_id)
        if run(("systemctl", "--user", "stop", service), None).returncode != 0:
            raise InstallerError("plugin_service_stop_failed", "plugin service could not be stopped for activation")
        stopped.append(
            {
                "plugin_id": plugin_id,
                "active": active,
                "enabled": enabled,
                "receipt_status": status.get("receipt_status"),
            }
        )
    return stopped


def restore_plugin_service_states(
    layout: InstallLayout,
    states: Iterable[Mapping[str, object]],
    *,
    allowed_ids: Iterable[str] | None = None,
    run: RunCommand = _run,
) -> None:
    allowed = None if allowed_ids is None else set(allowed_ids)
    for state in states:
        plugin_id = _plugin_service_id(str(state.get("plugin_id") or ""))
        if allowed is not None and plugin_id not in allowed:
            continue
        if not plugin_service_unit_is_owned(layout, plugin_id):
            raise InstallerError("plugin_service_unsafe", "plugin service unit could not be restored safely")
        service = plugin_service_name(plugin_id)
        current = status_plugin_service(layout, plugin_id, run=run)
        if state.get("enabled") is True:
            if current.get("active") is True and current.get("enabled") is True:
                continue
            command = ("systemctl", "--user", "enable", "--now", service)
        elif state.get("active") is True:
            if current.get("active") is True:
                continue
            command = ("systemctl", "--user", "start", service)
        else:
            continue
        if run(command, None).returncode != 0:
            raise InstallerError("plugin_service_restore_failed", "plugin service state could not be restored")


def service_unit(layout: InstallLayout) -> bytes:
    return (
        "[Unit]\n"
        "Description=Dispatch\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        "UMask=0077\n"
        f"ExecStart={_quote_systemd(str(layout.command_path))} service\n"
        f"Environment=DISPATCH_HOME={_quote_systemd(str(layout.dispatch_home))}\n"
        f"Environment=PLAYWRIGHT_BROWSERS_PATH={_quote_systemd(str(layout.browser_cache))}\n"
        "Restart=on-failure\n"
        "RestartSec=2\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    ).encode("utf-8")


def _previous_service_unit(layout: InstallLayout) -> bytes:
    return service_unit(layout).replace(b"UMask=0077\n", b"", 1)


def _clone_service_unit(layout: InstallLayout) -> bytes:
    """Clone-lifecycle service used before Browser Manager cache ownership."""
    return service_unit(layout).replace(
        str(layout.browser_cache).encode("utf-8"),
        str(layout.legacy_browser_cache).encode("utf-8"),
        1,
    )


def _previous_clone_service_unit(layout: InstallLayout) -> bytes:
    return _clone_service_unit(layout).replace(b"UMask=0077\n", b"", 1)


def legacy_service_unit(layout: InstallLayout) -> bytes:
    launcher = layout.dispatch_home / "bin" / "dispatch"
    quoted_launcher = '"' + str(launcher).replace("\\", "\\\\").replace('"', '\\"') + '"'
    quoted_home = '"' + str(layout.dispatch_home).replace("\\", "\\\\").replace('"', '\\"') + '"'
    return (
        "[Unit]\n"
        "Description=Dispatch Core\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={quoted_launcher} service\n"
        f"Environment=DISPATCH_HOME={quoted_home}\n"
        "Restart=on-failure\n"
        "RestartSec=2\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    ).encode("utf-8")


def _atomic_bytes(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    ensure_private_directory(path.parent, "service directory")
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, mode)
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        path.chmod(mode)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _record_matches(layout: InstallLayout, content: bytes) -> bool:
    try:
        payload = read_json(layout.state / "service.json")
    except InstallerError:
        return False
    return (
        set(payload) == {"schema_version", "unit", "unit_sha256", "service", "contains_secrets"}
        and payload.get("schema_version") == 1
        and payload.get("unit") == str(layout.service_path)
        and payload.get("service") == "dispatch.service"
        and payload.get("contains_secrets") is False
        and payload.get("unit_sha256") == hashlib.sha256(content).hexdigest()
    )


def service_unit_is_owned(layout: InstallLayout) -> bool:
    path = layout.service_path
    try:
        assert_user_owned_directory(layout.service_directory, "service directory")
    except InstallerError:
        return False
    if path.is_symlink() or not path.is_file():
        return False
    try:
        details = path.stat()
        if details.st_size > 64 * 1024:
            return False
        content = path.read_bytes()
    except OSError:
        return False
    return (
        details.st_uid == os.geteuid()
        and details.st_nlink == 1
        and stat.S_IMODE(details.st_mode) == 0o600
        and details.st_size <= 64 * 1024
        and content in {
            service_unit(layout),
            _previous_service_unit(layout),
            _clone_service_unit(layout),
            _previous_clone_service_unit(layout),
        }
        and _record_matches(layout, content)
    )


def legacy_service_unit_is_owned(layout: InstallLayout) -> bool:
    unit = layout.service_directory / "dispatch-core.service"
    receipt_path = layout.state / "install" / "service.json"
    try:
        assert_user_owned_directory(layout.service_directory, "service directory")
        assert_user_owned_directory(receipt_path.parent, "legacy installation state")
    except InstallerError:
        return False
    if unit.is_symlink() or not unit.is_file():
        return False
    try:
        details = unit.stat()
        receipt = read_json(receipt_path, maximum=16 * 1024)
        content = unit.read_bytes()
    except (OSError, InstallerError):
        return False
    return (
        details.st_uid == os.geteuid()
        and details.st_nlink == 1
        and stat.S_IMODE(details.st_mode) == 0o600
        and set(receipt)
        == {"schema_version", "unit", "unit_sha256", "launcher", "service", "status", "contains_secrets"}
        and receipt.get("schema_version") == 1
        and receipt.get("unit") == str(unit)
        and receipt.get("launcher") == str(layout.dispatch_home / "bin" / "dispatch")
        and receipt.get("service") == "dispatch-core.service"
        and receipt.get("status") == "active"
        and receipt.get("contains_secrets") is False
        and content == legacy_service_unit(layout)
        and receipt.get("unit_sha256") == hashlib.sha256(content).hexdigest()
    )


def install_user_service(
    layout: InstallLayout,
    *,
    run: RunCommand = _run,
    activate: bool = True,
) -> dict[str, object]:
    ensure_private_directory(layout.service_directory, "service directory")
    content = service_unit(layout)
    if layout.service_path.exists() or layout.service_path.is_symlink():
        if layout.service_path.is_symlink() or not layout.service_path.is_file():
            raise InstallerError("service_conflict", "existing service unit is not Dispatch-owned")
        details = layout.service_path.stat()
        if details.st_size > 64 * 1024:
            raise InstallerError("service_conflict", "existing service unit is not Dispatch-owned")
        if not service_unit_is_owned(layout):
            raise InstallerError("service_conflict", "existing service unit is not Dispatch-owned")
    _atomic_bytes(layout.service_path, content)
    record = {
        "schema_version": 1,
        "unit": str(layout.service_path),
        "unit_sha256": hashlib.sha256(content).hexdigest(),
        "service": "dispatch.service",
        "contains_secrets": False,
    }
    atomic_json(layout.state / "service.json", record)
    if activate:
        for command in (
            ("systemctl", "--user", "daemon-reload"),
            ("systemctl", "--user", "enable", "--now", "dispatch.service"),
            ("systemctl", "--user", "restart", "dispatch.service"),
        ):
            completed = run(command, None)
            if completed.returncode != 0:
                raise InstallerError("service_activation_failed", "Dispatch user service could not be activated and restarted")
    return {"status": "active" if activate else "prepared", "unit": str(layout.service_path), "service": "dispatch.service"}


def inspect_user_service(layout: InstallLayout, *, run: RunCommand = _run) -> dict[str, object]:
    try:
        assert_user_owned_directory(layout.service_directory, "service directory")
    except InstallerError as exc:
        return {"status": "unsafe", "service": "dispatch.service", "unit": str(layout.service_path), "error": str(exc)[:256]}
    if not layout.service_path.exists() and not layout.service_path.is_symlink():
        return {"status": "missing", "service": "dispatch.service", "unit": str(layout.service_path)}
    try:
        if not service_unit_is_owned(layout):
            raise InstallerError(
                "service_unit_unsafe",
                "Dispatch service unit is unsafe or differs from its record",
            )
        active = run(("systemctl", "--user", "is-active", "--quiet", "dispatch.service"), None).returncode == 0
        enabled = run(("systemctl", "--user", "is-enabled", "--quiet", "dispatch.service"), None).returncode == 0
        return {
            "status": "ready" if active and enabled else "incomplete",
            "service": "dispatch.service",
            "unit": str(layout.service_path),
            "active": active,
            "enabled": enabled,
        }
    except (OSError, InstallerError, ValueError) as exc:
        return {"status": "unsafe", "service": "dispatch.service", "unit": str(layout.service_path), "error": str(exc)[:256]}


def remove_user_service(layout: InstallLayout, *, run: RunCommand = _run) -> None:
    if not layout.service_path.exists() and not layout.service_path.is_symlink():
        return
    assert_user_owned_directory(layout.service_directory, "service directory")
    if not service_unit_is_owned(layout):
        raise InstallerError("service_unit_unsafe", "Dispatch service unit is not Dispatch-owned")
    completed = run(("systemctl", "--user", "disable", "--now", "dispatch.service"), None)
    if completed.returncode != 0:
        raise InstallerError("service_stop_failed", "Dispatch user service could not be stopped")
    content = layout.service_path.read_bytes()
    layout.service_path.unlink()
    reload_result = run(("systemctl", "--user", "daemon-reload"), None)
    if reload_result.returncode != 0:
        rollback_failure: BaseException | None = None
        try:
            _atomic_bytes(layout.service_path, content)
        except BaseException as exc:
            rollback_failure = exc
        for command in (
            ("systemctl", "--user", "daemon-reload"),
            ("systemctl", "--user", "enable", "--now", "dispatch.service"),
        ):
            try:
                result = run(command, None)
                if result.returncode != 0:
                    raise InstallerError("service_rollback_command_failed", "service rollback command failed")
            except BaseException as exc:
                if rollback_failure is None:
                    rollback_failure = exc
        if rollback_failure is not None:
            raise InstallerError(
                "service_rollback_failed",
                "service removal failed and the previous service could not be fully restored",
            ) from rollback_failure
        raise InstallerError("service_reload_failed", "systemd user manager could not reload")
    (layout.state / "service.json").unlink(missing_ok=True)


def stop_legacy_user_service(layout: InstallLayout, *, run: RunCommand = _run) -> bool:
    path = layout.service_directory / "dispatch-core.service"
    if not path.exists() and not path.is_symlink():
        return False
    assert_user_owned_directory(layout.service_directory, "service directory")
    if not legacy_service_unit_is_owned(layout):
        raise InstallerError("legacy_service_unsafe", "legacy service unit is not Dispatch-owned")
    completed = run(("systemctl", "--user", "disable", "--now", "dispatch-core.service"), None)
    if completed.returncode != 0:
        raise InstallerError("legacy_service_stop_failed", "legacy Dispatch service could not be stopped")
    return True


def remove_legacy_user_service(layout: InstallLayout, *, run: RunCommand = _run) -> None:
    path = layout.service_directory / "dispatch-core.service"
    if not stop_legacy_user_service(layout, run=run):
        return
    receipt = layout.state / "install" / "service.json"
    assert_user_owned_directory(receipt.parent, "legacy installation state")
    content = path.read_bytes()
    path.unlink()
    reload_result = run(("systemctl", "--user", "daemon-reload"), None)
    if reload_result.returncode != 0:
        rollback_failure: BaseException | None = None
        try:
            _atomic_bytes(path, content)
        except BaseException as exc:
            rollback_failure = exc
        for command in (
            ("systemctl", "--user", "daemon-reload"),
            ("systemctl", "--user", "enable", "--now", "dispatch-core.service"),
        ):
            try:
                result = run(command, None)
                if result.returncode != 0:
                    raise InstallerError("legacy_service_rollback_command_failed", "legacy service rollback command failed")
            except BaseException as exc:
                if rollback_failure is None:
                    rollback_failure = exc
        if rollback_failure is not None:
            raise InstallerError(
                "legacy_service_rollback_failed",
                "legacy service cleanup failed and the previous service could not be fully restored",
            ) from rollback_failure
        raise InstallerError("legacy_service_reload_failed", "systemd user manager could not reload")
    receipt.unlink(missing_ok=True)
    try:
        receipt.parent.rmdir()
    except OSError:
        pass


__all__ = [
    "disable_plugin_service",
    "disable_plugin_services",
    "enable_plugin_service",
    "enable_plugin_services",
    "inspect_plugin_services",
    "install_user_service",
    "inspect_user_service",
    "legacy_service_unit_is_owned",
    "plugin_service_name",
    "plugin_service_ids",
    "plugin_service_path",
    "plugin_service_receipt_path",
    "plugin_service_unit",
    "plugin_service_unit_is_owned",
    "prepare_plugin_service",
    "prepare_plugin_services",
    "remove_plugin_service",
    "remove_plugin_services",
    "restore_plugin_service_states",
    "status_plugin_service",
    "status_plugin_services",
    "stop_plugin_services_for_activation",
    "remove_legacy_user_service",
    "remove_user_service",
    "service_unit_is_owned",
    "service_unit",
    "stop_legacy_user_service",
]
