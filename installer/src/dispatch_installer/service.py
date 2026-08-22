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
    ensure_private_directory,
    read_json,
)

RunCommand = Callable[[Sequence[str], Path | None], subprocess.CompletedProcess[str]]

_PLUGIN_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


def _run(command: Sequence[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, check=False, capture_output=True, text=True)


def systemd_service_state(service: str, *, run: RunCommand = _run) -> dict[str, bool]:
    return {
        "active": run(("systemctl", "--user", "is-active", "--quiet", service), None).returncode == 0,
        "enabled": run(("systemctl", "--user", "is-enabled", "--quiet", service), None).returncode == 0,
    }


def restore_systemd_service_state(
    service: str,
    state: Mapping[str, object],
    *,
    run: RunCommand = _run,
) -> None:
    enable_command = "enable" if state.get("enabled") is True else "disable"
    if run(("systemctl", "--user", enable_command, service), None).returncode != 0:
        raise InstallerError("service_restore_failed", "service enablement state could not be restored")
    active_command = "start" if state.get("active") is True else "stop"
    if run(("systemctl", "--user", active_command, service), None).returncode != 0:
        raise InstallerError("service_restore_failed", "service activity state could not be restored")


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


def _remove_obsolete_plugin_receipt(
    layout: InstallLayout,
    plugin_id: str,
    content: bytes,
    *,
    remove: bool = True,
) -> None:
    path = plugin_service_receipt_path(layout, plugin_id)
    if not path.exists() and not path.is_symlink():
        return
    try:
        assert_user_owned_directory(path.parent, "plugin service state")
        details = path.lstat()
        payload = read_json(path, maximum=16 * 1024)
    except (InstallerError, OSError) as exc:
        raise InstallerError("plugin_service_unsafe", "obsolete plugin service receipt is unsafe") from exc
    expected = {"schema_version", "plugin_id", "unit", "unit_sha256", "service", "status", "contains_secrets"}
    if (
        path.is_symlink()
        or not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.geteuid()
        or details.st_nlink != 1
        or stat.S_IMODE(details.st_mode) != 0o600
        or set(payload) != expected
        or payload.get("schema_version") != 1
        or payload.get("plugin_id") != plugin_id
        or payload.get("unit") != str(plugin_service_path(layout, plugin_id))
        or payload.get("unit_sha256") != hashlib.sha256(content).hexdigest()
        or payload.get("service") != plugin_service_name(plugin_id)
        or payload.get("status") not in {"prepared", "enabled", "disabled"}
        or payload.get("contains_secrets") is not False
    ):
        raise InstallerError("plugin_service_unsafe", "obsolete plugin service receipt is unsafe")
    if remove:
        path.unlink()


def plugin_service_unit_is_owned(layout: InstallLayout, plugin_id: str) -> bool:
    try:
        plugin_id = _plugin_service_id(plugin_id)
        assert_user_owned_directory(layout.service_directory, "service directory")
        path = plugin_service_path(layout, plugin_id)
        if path.is_symlink() or not path.is_file():
            return False
        details = path.stat(follow_symlinks=False)
        return (
            details.st_uid == os.geteuid()
            and details.st_nlink == 1
            and stat.S_IMODE(details.st_mode) == 0o600
            and details.st_size <= 64 * 1024
            and path.read_bytes() == plugin_service_unit(layout, plugin_id)
        )
    except (InstallerError, OSError):
        return False


def _write_plugin_service(layout: InstallLayout, plugin_id: str) -> dict[str, object]:
    plugin_id = _plugin_service_id(plugin_id)
    ensure_private_directory(layout.service_directory, "service directory")
    path = plugin_service_path(layout, plugin_id)
    if path.exists() or path.is_symlink():
        if not plugin_service_unit_is_owned(layout, plugin_id):
            raise InstallerError("plugin_service_conflict", "existing plugin service unit is not Dispatch-owned")
    content = plugin_service_unit(layout, plugin_id)
    _remove_obsolete_plugin_receipt(layout, plugin_id, content)
    _atomic_bytes(path, content)
    return {
        "status": "prepared",
        "plugin_id": plugin_id,
        "unit": str(path),
        "service": plugin_service_name(plugin_id),
    }


def prepare_plugin_service(layout: InstallLayout, plugin_id: str) -> dict[str, object]:
    """Publish a disabled service projection without starting it."""

    plugin_id = _plugin_service_id(plugin_id)
    return _write_plugin_service(layout, plugin_id)


def _require_selected_auth_profile(manager: object, plugin_id: str, provider: str) -> None:
    try:
        manager.profile_for_plugin(plugin_id, provider)  # type: ignore[attr-defined]
    except Exception:
        compatible = manager.compatible_profiles(provider)  # type: ignore[attr-defined]
        if len(compatible) != 1 or not isinstance(compatible[0].get("profile"), str):
            raise
        manager.bind_profile(str(compatible[0]["profile"]), plugin_id, provider)  # type: ignore[attr-defined]


def _require_plugin_auth_profile(layout: InstallLayout, plugin_id: str) -> None:
    """Require each install-validated profile before a service may start."""
    from .setup import load_plugin_config

    config = load_plugin_config(layout)
    required: list[dict[str, object]] = []
    for plugin in config.get("plugins", []):
        if isinstance(plugin, dict) and plugin.get("id") == plugin_id and isinstance(plugin.get("required_profiles"), list):
            required = [item for item in plugin["required_profiles"] if isinstance(item, dict)]
            break
    if not required:
        return
    try:
        from .setup import assert_source_project_safe

        core_root = assert_source_project_safe(layout.clone / "dispatch-core")
        import sys

        if str(core_root) not in sys.path:
            sys.path.insert(0, str(core_root))
        from authentication import AuthenticationManager
        from paths import DispatchPaths

        environment = {
            **os.environ,
            "DISPATCH_HOME": str(layout.dispatch_home),
            "DISPATCH_CODE_ROOT": str(layout.clone),
            "DISPATCH_CONFIG_ROOT": str(layout.config),
            "DISPATCH_SECRETS_ROOT": str(layout.secrets),
            "DISPATCH_DATA_ROOT": str(layout.data),
            "DISPATCH_STATE_ROOT": str(layout.state),
            "DISPATCH_CACHE_ROOT": str(layout.cache),
            "DISPATCH_LOGS_ROOT": str(layout.logs),
            "DISPATCH_RUNTIME_ROOT": str(layout.run),
        }
        manager = AuthenticationManager(DispatchPaths.from_environment(environment, code_root=layout.clone))
        for item in required:
            provider = item.get("provider")
            if not isinstance(provider, str):
                raise InstallerError("plugin_authentication_invalid", "plugin required profile metadata is invalid")
            _require_selected_auth_profile(manager, plugin_id, provider)
    except InstallerError:
        raise
    except Exception as exc:
        raise InstallerError(
            "plugin_auth_profile_required",
            "an enrolled authentication profile is required before the plugin service can be enabled",
        ) from exc


def enable_plugin_service(
    layout: InstallLayout,
    plugin_id: str,
    *,
    run: RunCommand = _run,
) -> dict[str, object]:
    plugin_id = _plugin_service_id(plugin_id)
    path = plugin_service_path(layout, plugin_id)
    previous_content: bytes | None = None
    previous_state = {"active": False, "enabled": False}
    if path.exists() or path.is_symlink():
        if not plugin_service_unit_is_owned(layout, plugin_id):
            raise InstallerError("plugin_service_unsafe", "plugin service unit is not Dispatch-owned")
        previous_content = path.read_bytes()
        previous_state = systemd_service_state(plugin_service_name(plugin_id), run=run)
    systemd_touched = False
    try:
        prepared = _write_plugin_service(layout, plugin_id)
        _require_plugin_auth_profile(layout, plugin_id)
        service = plugin_service_name(plugin_id)
        if run((str(layout.command_path), "plugin", "health", plugin_id), None).returncode != 0:
            raise InstallerError(
                "plugin_service_not_ready",
                "plugin configuration health must pass before its service can be enabled",
            )
        for command in (
            ("systemctl", "--user", "daemon-reload"),
            ("systemctl", "--user", "enable", "--now", service),
            ("systemctl", "--user", "restart", service),
        ):
            systemd_touched = True
            if run(command, None).returncode != 0:
                raise InstallerError("plugin_service_activation_failed", "plugin service could not be enabled and restarted")
        current = status_plugin_service(layout, plugin_id, run=run)
        if current.get("active") is not True or current.get("enabled") is not True:
            raise InstallerError("plugin_service_activation_failed", "plugin service did not remain active and enabled")
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
        if previous_content is None:
            attempt(lambda: path.unlink(missing_ok=True))
        else:
            attempt(lambda: _atomic_bytes(path, previous_content))
        if systemd_touched:
            attempt(lambda: run(("systemctl", "--user", "daemon-reload"), None))
        if previous_content is not None and systemd_touched:
            attempt(lambda: restore_systemd_service_state(plugin_service_name(plugin_id), previous_state, run=run))
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
        _remove_obsolete_plugin_receipt(
            layout,
            plugin_id,
            plugin_service_unit(layout, plugin_id),
        )
        return {"status": "missing", "plugin_id": plugin_id, "unit": str(path), "service": plugin_service_name(plugin_id)}
    if not plugin_service_unit_is_owned(layout, plugin_id):
        raise InstallerError("plugin_service_unsafe", "plugin service unit is not Dispatch-owned")
    service = plugin_service_name(plugin_id)
    if run(("systemctl", "--user", "disable", "--now", service), None).returncode != 0:
        raise InstallerError("plugin_service_stop_failed", "plugin service could not be disabled")
    _remove_obsolete_plugin_receipt(layout, plugin_id, path.read_bytes())
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
        return {"status": "missing", "plugin_id": plugin_id, "unit": str(path), "service": plugin_service_name(plugin_id)}
    if not plugin_service_unit_is_owned(layout, plugin_id):
        return {"status": "unsafe", "plugin_id": plugin_id, "unit": str(path), "service": plugin_service_name(plugin_id)}
    service = plugin_service_name(plugin_id)
    state = systemd_service_state(service, run=run)
    if state["active"] and state["enabled"]:
        status = "ready"
    elif not state["active"] and not state["enabled"]:
        status = "prepared"
    else:
        status = "incomplete"
    return {
        "status": status,
        "plugin_id": plugin_id,
        "unit": str(path),
        "service": service,
        **state,
    }


def remove_plugin_service(
    layout: InstallLayout,
    plugin_id: str,
    *,
    run: RunCommand = _run,
) -> None:
    plugin_id = _plugin_service_id(plugin_id)
    path = plugin_service_path(layout, plugin_id)
    if not path.exists() and not path.is_symlink():
        _remove_obsolete_plugin_receipt(
            layout,
            plugin_id,
            plugin_service_unit(layout, plugin_id),
        )
        return
    if not plugin_service_unit_is_owned(layout, plugin_id):
        raise InstallerError("plugin_service_unsafe", "plugin service unit is not Dispatch-owned")
    service = plugin_service_name(plugin_id)
    content = path.read_bytes()
    _remove_obsolete_plugin_receipt(layout, plugin_id, content, remove=False)
    previous_state = systemd_service_state(service, run=run)
    try:
        if run(("systemctl", "--user", "disable", "--now", service), None).returncode != 0:
            raise InstallerError("plugin_service_stop_failed", "plugin service could not be stopped")
        path.unlink()
        if run(("systemctl", "--user", "daemon-reload"), None).returncode != 0:
            raise InstallerError("plugin_service_reload_failed", "systemd user manager could not reload")
    except BaseException as primary:
        rollback_failure: BaseException | None = None
        try:
            if not path.exists() and not path.is_symlink():
                _atomic_bytes(path, content)
        except BaseException as exc:
            rollback_failure = exc
        try:
            if run(("systemctl", "--user", "daemon-reload"), None).returncode != 0:
                raise InstallerError("plugin_service_rollback_failed", "systemd reload failed during plugin service rollback")
            restore_systemd_service_state(service, previous_state, run=run)
        except BaseException as exc:
            rollback_failure = rollback_failure or exc
        if rollback_failure is not None:
            raise InstallerError(
                "plugin_service_rollback_failed",
                "plugin service removal failed and its previous state could not be restored",
            ) from rollback_failure
        raise primary
    _remove_obsolete_plugin_receipt(layout, plugin_id, content)


def inspect_plugin_services(
    layout: InstallLayout,
    plugin_ids: Iterable[str] = (),
    *,
    run: RunCommand = _run,
) -> dict[str, object]:
    required = set(plugin_ids)
    try:
        ids = plugin_service_ids(layout, required)
    except InstallerError as exc:
        return {"status": "unsafe", "services": {}, "error": str(exc)[:256]}
    services: dict[str, object] = {}
    for plugin_id in sorted(ids):
        try:
            services[plugin_id] = status_plugin_service(layout, plugin_id, run=run)
        except (InstallerError, OSError) as exc:
            services[plugin_id] = {"status": "unsafe", "plugin_id": plugin_id, "error": str(exc)[:256]}
    healthy = all(
        isinstance(item, dict)
        and item.get("status") in {"ready", "prepared", "missing"}
        and not (plugin_id in required and item.get("status") == "missing")
        for plugin_id, item in services.items()
    )
    return {"status": "ready" if healthy else "unsafe", "services": services}


def plugin_service_ids(layout: InstallLayout, plugin_ids: Iterable[str] = ()) -> set[str]:
    ids = {_plugin_service_id(plugin_id) for plugin_id in plugin_ids}
    receipt_root = layout.state / "plugins" / "services"
    if receipt_root.exists() or receipt_root.is_symlink():
        assert_user_owned_directory(receipt_root, "plugin service state")
        for receipt in receipt_root.glob("*.json"):
            details = receipt.lstat()
            if (
                receipt.is_symlink()
                or not stat.S_ISREG(details.st_mode)
                or details.st_uid != os.geteuid()
                or details.st_nlink != 1
                or stat.S_IMODE(details.st_mode) != 0o600
            ):
                raise InstallerError("plugin_service_unsafe", "obsolete plugin service receipt is unsafe")
            ids.add(_plugin_service_id(receipt.stem))
    if layout.service_directory.exists() or layout.service_directory.is_symlink():
        assert_user_owned_directory(layout.service_directory, "service directory")
        for unit in layout.service_directory.glob("dispatch-plugin-*.service"):
            name = unit.name.removeprefix("dispatch-plugin-").removesuffix(".service")
            ids.add(_plugin_service_id(name))
    return ids


def stop_plugin_services_for_activation(
    layout: InstallLayout,
    plugin_ids: Iterable[str],
    *,
    run: RunCommand = _run,
) -> list[dict[str, object]]:
    """Stop every active/enabled owned plugin service and retain its exact state."""
    stopped: list[dict[str, object]] = []
    try:
        for plugin_id in sorted(plugin_service_ids(layout, plugin_ids)):
            status = status_plugin_service(layout, plugin_id, run=run)
            if status.get("status") == "unsafe":
                raise InstallerError("plugin_service_unsafe", "plugin service unit is not Dispatch-owned")
            active = status.get("active") is True
            enabled = status.get("enabled") is True
            if not active and not enabled:
                continue
            service = plugin_service_name(plugin_id)
            stopped.append(
                {
                    "plugin_id": plugin_id,
                    "active": active,
                    "enabled": enabled,
                }
            )
            if run(("systemctl", "--user", "stop", service), None).returncode != 0:
                raise InstallerError("plugin_service_stop_failed", "plugin service could not be stopped for activation")
    except BaseException as primary:
        # Best-effort per-id restore (audit M-3): one id failing to come
        # back must not abandon the remaining ids, and the PRIMARY stop
        # failure must stay the reported error. The old all-or-nothing
        # restore replaced it with a generic plugin_service_rollback_failed
        # (even for KeyboardInterrupt) while leaving later services down.
        rollback_failed = False
        for state in list(stopped):
            try:
                restore_plugin_service_states(layout, [state], run=run)
            except BaseException:
                rollback_failed = True
        if rollback_failed:
            if isinstance(primary, InstallerError):
                raise InstallerError(
                    "plugin_service_rollback_failed",
                    "plugin service stop failed; some previously running plugin services could not be restored",
                ) from primary
            # Interrupts and other control-flow exceptions keep their type:
            # an abort must surface as an abort even when cleanup struggled.
            raise primary
        raise
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
        expected_active = state.get("active") is True
        expected_enabled = state.get("enabled") is True
        if (
            current.get("active") is expected_active
            and current.get("enabled") is expected_enabled
        ):
            continue
        restore_systemd_service_state(
            service,
            {"active": expected_active, "enabled": expected_enabled},
            run=run,
        )


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


def _remove_obsolete_main_service_record(
    layout: InstallLayout,
    content: bytes,
    *,
    remove: bool = True,
) -> None:
    path = layout.state / "service.json"
    if not path.exists() and not path.is_symlink():
        return
    try:
        assert_user_owned_directory(layout.state, "Dispatch state directory")
        details = path.lstat()
        payload = read_json(path, maximum=16 * 1024)
    except (InstallerError, OSError) as exc:
        raise InstallerError("service_record_unsafe", "obsolete Dispatch service record is unsafe") from exc
    expected = {"schema_version", "unit", "unit_sha256", "service", "contains_secrets"}
    if (
        path.is_symlink()
        or not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.geteuid()
        or details.st_nlink != 1
        or stat.S_IMODE(details.st_mode) != 0o600
        or set(payload) != expected
        or payload.get("schema_version") != 1
        or payload.get("unit") != str(layout.service_path)
        or payload.get("unit_sha256") != hashlib.sha256(content).hexdigest()
        or payload.get("service") != "dispatch.service"
        or payload.get("contains_secrets") is not False
    ):
        raise InstallerError("service_record_unsafe", "obsolete Dispatch service record is unsafe")
    if remove:
        path.unlink()


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
    _remove_obsolete_main_service_record(layout, content)
    _atomic_bytes(layout.service_path, content)
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
                "Dispatch service unit is unsafe or differs from a recognized Dispatch projection",
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
    content = layout.service_path.read_bytes()
    _remove_obsolete_main_service_record(layout, content, remove=False)
    previous_state = systemd_service_state("dispatch.service", run=run)
    try:
        if run(("systemctl", "--user", "disable", "--now", "dispatch.service"), None).returncode != 0:
            raise InstallerError("service_stop_failed", "Dispatch user service could not be stopped")
        layout.service_path.unlink()
        if run(("systemctl", "--user", "daemon-reload"), None).returncode != 0:
            raise InstallerError("service_reload_failed", "systemd user manager could not reload")
    except BaseException as primary:
        rollback_failure: BaseException | None = None
        try:
            if not layout.service_path.exists() and not layout.service_path.is_symlink():
                _atomic_bytes(layout.service_path, content)
        except BaseException as exc:
            rollback_failure = exc
        try:
            result = run(("systemctl", "--user", "daemon-reload"), None)
            if result.returncode != 0:
                raise InstallerError("service_rollback_command_failed", "service rollback command failed")
            restore_systemd_service_state("dispatch.service", previous_state, run=run)
        except BaseException as exc:
            rollback_failure = rollback_failure or exc
        if rollback_failure is not None:
            raise InstallerError(
                "service_rollback_failed",
                "service removal failed and the previous service could not be fully restored",
            ) from rollback_failure
        raise primary
    _remove_obsolete_main_service_record(layout, content)


def stop_legacy_user_service(layout: InstallLayout, *, run: RunCommand = _run) -> bool:
    path = layout.service_directory / "dispatch-core.service"
    if not path.exists() and not path.is_symlink():
        return False
    assert_user_owned_directory(layout.service_directory, "service directory")
    if not legacy_service_unit_is_owned(layout):
        raise InstallerError("legacy_service_unsafe", "legacy service unit is not Dispatch-owned")
    previous_state = systemd_service_state("dispatch-core.service", run=run)
    try:
        if run(("systemctl", "--user", "disable", "--now", "dispatch-core.service"), None).returncode != 0:
            raise InstallerError("legacy_service_stop_failed", "legacy Dispatch service could not be stopped")
    except BaseException as primary:
        try:
            restore_systemd_service_state("dispatch-core.service", previous_state, run=run)
        except BaseException as rollback_error:
            raise InstallerError(
                "legacy_service_rollback_failed",
                "legacy service stop failed and its previous state could not be restored",
            ) from rollback_error
        raise primary
    return True


def remove_legacy_user_service(layout: InstallLayout, *, run: RunCommand = _run) -> None:
    path = layout.service_directory / "dispatch-core.service"
    if not path.exists() and not path.is_symlink():
        return
    assert_user_owned_directory(layout.service_directory, "service directory")
    if not legacy_service_unit_is_owned(layout):
        raise InstallerError("legacy_service_unsafe", "legacy service unit is not Dispatch-owned")
    receipt = layout.state / "install" / "service.json"
    assert_user_owned_directory(receipt.parent, "legacy installation state")
    content = path.read_bytes()
    previous_state = systemd_service_state("dispatch-core.service", run=run)
    try:
        if not stop_legacy_user_service(layout, run=run):
            return
        path.unlink()
        if run(("systemctl", "--user", "daemon-reload"), None).returncode != 0:
            raise InstallerError("legacy_service_reload_failed", "systemd user manager could not reload")
    except BaseException as primary:
        rollback_failure: BaseException | None = None
        try:
            if not path.exists() and not path.is_symlink():
                _atomic_bytes(path, content)
        except BaseException as exc:
            rollback_failure = exc
        try:
            if run(("systemctl", "--user", "daemon-reload"), None).returncode != 0:
                raise InstallerError("legacy_service_rollback_command_failed", "legacy service rollback command failed")
            restore_systemd_service_state("dispatch-core.service", previous_state, run=run)
        except BaseException as exc:
            rollback_failure = rollback_failure or exc
        if rollback_failure is not None:
            raise InstallerError(
                "legacy_service_rollback_failed",
                "legacy service cleanup failed and the previous service could not be fully restored",
            ) from rollback_failure
        raise primary
    receipt.unlink(missing_ok=True)
    try:
        receipt.parent.rmdir()
    except OSError:
        pass


__all__ = [
    "disable_plugin_service",
    "enable_plugin_service",
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
    "remove_plugin_service",
    "restore_plugin_service_states",
    "restore_systemd_service_state",
    "status_plugin_service",
    "stop_plugin_services_for_activation",
    "remove_legacy_user_service",
    "remove_user_service",
    "service_unit_is_owned",
    "service_unit",
    "stop_legacy_user_service",
    "systemd_service_state",
]
