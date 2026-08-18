"""Private filesystem, locking, and Playwright Chromium runtime."""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import signal
import stat
import threading
import time
from typing import Any, Callable, Iterator, Protocol

from paths import DispatchPaths, require_within

from .models import (
    BrowserManagerError,
    BrowserMode,
    BrowserRealm,
    ManagedBrowserSession,
)
from .runtime_authority import (
    BrowserRuntimeAuthority,
    BrowserRuntimeIdentity,
)


@dataclass(frozen=True, slots=True)
class BrowserLayout:
    database: Path
    profiles: Path
    runtime: Path
    locks: Path
    data_boundary: Path
    state_boundary: Path
    runtime_boundary: Path

    @classmethod
    def from_paths(cls, paths: DispatchPaths) -> "BrowserLayout":
        return cls(
            database=require_within(
                paths.data / "db" / "browser-manager" / "browser-manager.sqlite3",
                paths.data,
                "browser manager database",
            ),
            profiles=require_within(
                paths.state / "browser-manager" / "profiles",
                paths.state,
                "browser profiles",
            ),
            runtime=require_within(
                paths.runtime / "browser-manager",
                paths.runtime,
                "browser runtime",
            ),
            locks=require_within(
                paths.runtime / "browser-manager" / "locks",
                paths.runtime,
                "browser locks",
            ),
            data_boundary=paths.data,
            state_boundary=paths.state,
            runtime_boundary=paths.runtime,
        )

    def prepare(self) -> None:
        _private_directory(self.database.parent, self.data_boundary)
        _private_directory(self.profiles, self.state_boundary)
        _private_directory(self.runtime, self.runtime_boundary)
        _private_directory(self.locks, self.runtime_boundary)

    def profile(self, realm: str, plugin_id: str, account_alias: str) -> Path:
        profile = require_within(
            self.profiles / realm / plugin_id / account_alias,
            self.profiles,
            "browser profile",
        )
        return _private_directory(profile, self.state_boundary)

    def lock_path(self, kind: str, identity: str) -> Path:
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return require_within(self.locks / f"{kind}-{digest}.lock", self.locks, "browser lock")

    @property
    def generation_lock(self) -> Path:
        return require_within(
            self.state_boundary / "browser-manager" / "generation.lock",
            self.state_boundary,
            "browser generation lock",
        )


def _validate_private_ancestors(boundary: Path) -> None:
    absolute = Path(os.path.abspath(boundary))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if not current.exists() and not current.is_symlink():
            current.mkdir(mode=0o700)
            current.chmod(0o700)
            continue
        details = current.lstat()
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
            raise BrowserManagerError("unsafe_browser_storage", "private root ancestry is unsafe")
        if details.st_uid not in {0, os.geteuid()}:
            raise BrowserManagerError("unsafe_browser_storage", "private root ancestry has an unsafe owner")
        writable = details.st_mode & 0o022
        trusted_sticky_root = details.st_uid == 0 and bool(details.st_mode & stat.S_ISVTX)
        if writable and not trusted_sticky_root:
            raise BrowserManagerError("unsafe_browser_storage", "private root ancestry is group/world writable")


def _private_directory(path: Path, boundary: Path) -> Path:
    _validate_private_ancestors(boundary)
    path = require_within(path, boundary, "private browser directory")
    boundary = boundary.resolve(strict=False)
    if boundary.exists() and boundary.is_symlink():
        raise BrowserManagerError("unsafe_browser_storage", "private root cannot be a symlink")
    if not boundary.exists():
        boundary.mkdir(parents=True, mode=0o700)
    if not boundary.is_dir():
        raise BrowserManagerError("unsafe_browser_storage", "private root is not a directory")

    current = boundary
    for part in path.relative_to(boundary).parts:
        current = current / part
        if current.is_symlink():
            raise BrowserManagerError("unsafe_browser_storage", "private browser path cannot contain a symlink")
        if current.exists() and not current.is_dir():
            raise BrowserManagerError("unsafe_browser_storage", "private browser path is not a directory")
        if not current.exists():
            current.mkdir(mode=0o700)
        current.chmod(0o700)
    return path


def _open_pinned_lock(path: Path, flags: int) -> int:
    absolute = Path(os.path.abspath(path))
    if not absolute.is_absolute() or not absolute.name:
        raise OSError("lock path must be absolute")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    parent_descriptor = os.open("/", directory_flags)
    try:
        for part in absolute.parent.parts[1:]:
            child = os.open(part, directory_flags, dir_fd=parent_descriptor)
            os.close(parent_descriptor)
            parent_descriptor = child
        pinned = os.fstat(parent_descriptor)
        descriptor = os.open(absolute.name, flags, 0o600, dir_fd=parent_descriptor)
        try:
            current = absolute.parent.stat(follow_symlinks=False)
            if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino):
                raise OSError("lock parent changed identity")
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor
    finally:
        os.close(parent_descriptor)


class FileLock:
    """Non-blocking process lock held by an open file descriptor."""

    def __init__(self, path: Path, busy_code: str, *, shared: bool = False) -> None:
        self.path = path
        self.busy_code = busy_code
        self.shared = shared
        self._descriptor: int | None = None

    def acquire(self) -> None:
        flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = _open_pinned_lock(self.path, flags)
        except OSError as exc:
            raise BrowserManagerError("unsafe_browser_storage", "browser lock cannot be opened safely") from exc
        try:
            details = os.fstat(descriptor)
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_nlink != 1
                or details.st_uid != os.getuid()
            ):
                raise BrowserManagerError("unsafe_browser_storage", "browser lock is not a private regular file")
            operation = fcntl.LOCK_SH if self.shared else fcntl.LOCK_EX
            fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
            if os.fstat(descriptor).st_nlink != 1:
                raise BrowserManagerError("unsafe_browser_storage", "browser lock has an unsafe hard link")
            os.fchmod(descriptor, 0o600)
            if not self.shared:
                os.ftruncate(descriptor, 0)
                os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
        except BlockingIOError as exc:
            os.close(descriptor)
            raise BrowserManagerError(self.busy_code, "browser resource is already leased") from exc
        except BaseException:
            os.close(descriptor)
            raise
        self._descriptor = descriptor

    def release(self) -> None:
        if self._descriptor is None:
            return
        try:
            fcntl.flock(self._descriptor, fcntl.LOCK_UN)
        finally:
            os.close(self._descriptor)
            self._descriptor = None


@dataclass(slots=True)
class LeaseLocks:
    generation: FileLock
    slot: FileLock
    realm: FileLock
    profile: FileLock

    @classmethod
    def acquire(
        cls,
        layout: BrowserLayout,
        *,
        maximum_browsers: int,
        realm: str,
        profile_key: str,
    ) -> "LeaseLocks":
        generation_lock = FileLock(
            layout.generation_lock,
            "browser_generation_busy",
            shared=True,
        )
        generation_lock.acquire()
        slot: FileLock | None = None
        try:
            for index in range(maximum_browsers):
                candidate = FileLock(layout.lock_path("global", str(index)), "browser_capacity_unavailable")
                try:
                    candidate.acquire()
                except BrowserManagerError as exc:
                    if exc.code != "browser_capacity_unavailable":
                        raise
                    continue
                slot = candidate
                break
            if slot is None:
                raise BrowserManagerError("browser_capacity_unavailable", "all approved browser slots are occupied")

            realm_lock = FileLock(layout.lock_path("realm", realm), "browser_realm_busy")
            profile_lock = FileLock(layout.lock_path("profile", profile_key), "browser_profile_busy")
            try:
                realm_lock.acquire()
                profile_lock.acquire()
            except BaseException:
                profile_lock.release()
                realm_lock.release()
                slot.release()
                raise
        except BaseException:
            generation_lock.release()
            raise
        return cls(generation=generation_lock, slot=slot, realm=realm_lock, profile=profile_lock)

    def release(self) -> None:
        self.profile.release()
        self.realm.release()
        self.slot.release()
        self.generation.release()


class RuntimeHandle(Protocol):
    pid: int
    process_start_ticks: int
    session: ManagedBrowserSession
    identity: BrowserRuntimeIdentity

    def is_alive(self) -> bool: ...

    def close(self) -> None: ...


class BrowserRuntime(Protocol):
    @property
    def identity(self) -> BrowserRuntimeIdentity: ...

    def start(
        self,
        *,
        lease_id: str,
        profile: Path,
        realm: BrowserRealm,
        mode: BrowserMode,
        record_control_process: Callable[[int, int], None],
    ) -> RuntimeHandle: ...


@dataclass(slots=True)
class ProcessRuntimeHandle:
    context: Any
    playwright: Any
    profile: Path
    pid: int
    process_start_ticks: int
    session: ManagedBrowserSession
    identity: BrowserRuntimeIdentity
    control_pid: int
    control_process_start_ticks: int
    closed: bool = False

    def is_alive(self) -> bool:
        return not self.closed and process_start_ticks(self.pid) == self.process_start_ticks

    def close(self) -> None:
        if self.closed:
            return
        cleanup_error: BaseException | None = None
        try:
            for operation in (self.context.close, self.playwright.stop):
                try:
                    _call_bounded(operation, timeout_seconds=2)
                except BaseException as exc:
                    cleanup_error = cleanup_error or exc
            if process_start_ticks(self.pid) == self.process_start_ticks:
                try:
                    terminate_owned_process(
                        self.pid,
                        self.process_start_ticks,
                        self.profile,
                        self.identity.executable,
                    )
                except BaseException as exc:
                    cleanup_error = cleanup_error or exc
            if process_start_ticks(self.control_pid) == self.control_process_start_ticks:
                try:
                    terminate_control_process(
                        self.control_pid,
                        self.control_process_start_ticks,
                        self.identity.control_executable,
                    )
                except BaseException as exc:
                    cleanup_error = cleanup_error or exc
        finally:
            self.closed = True
        browser_alive = process_start_ticks(self.pid) == self.process_start_ticks
        control_alive = process_start_ticks(self.control_pid) == self.control_process_start_ticks
        if cleanup_error is not None and (browser_alive or control_alive):
            raise BrowserManagerError("browser_cleanup_failed", "owned browser process tree did not close") from cleanup_error
        if isinstance(cleanup_error, (KeyboardInterrupt, SystemExit)):
            raise cleanup_error


class PlaywrightRuntime:
    """Launch only the user-owned Playwright Chromium through Playwright."""

    __slots__ = ("__authority", "__installation", "__launch_executable")

    def __init__(self) -> None:
        self.__authority = BrowserRuntimeAuthority.production()
        self.__installation = self.__authority.load(full_tree=True)
        self.__launch_executable = self.__installation.identity.executable

    @property
    def identity(self) -> BrowserRuntimeIdentity:
        return self.__installation.identity

    def _verified_for_launch(self) -> VerifiedBrowserInstallation:
        current = self.__authority.load(full_tree=True)
        if current != self.__installation:
            raise BrowserManagerError(
                "browser_runtime_changed",
                "active browser runtime changed while Browser Manager was running",
            )
        return current

    def start(
        self,
        *,
        lease_id: str,
        profile: Path,
        realm: BrowserRealm,
        mode: BrowserMode,
        record_control_process: Callable[[int, int], None],
    ) -> ProcessRuntimeHandle:
        installation = self._verified_for_launch()
        identity = installation.identity
        playwright: Any | None = None
        context: Any | None = None
        control_pid: int | None = None
        control_ticks: int | None = None
        executable_descriptor: int | None = None
        try:
            executable = Path(os.path.abspath(self.__launch_executable))
            cache = installation.browsers_path
            if not executable.is_absolute():
                raise BrowserManagerError("browser_runtime_unsafe", "approved Chromium path must be absolute")
            try:
                relative = executable.relative_to(cache)
                current = cache
                for part in relative.parts:
                    current /= part
                    if current.is_symlink():
                        raise BrowserManagerError(
                            "browser_runtime_changed",
                            "approved Chromium path became aliased before launch",
                        )
                executable = executable.resolve(strict=True)
                executable.relative_to(cache)
                details = executable.stat(follow_symlinks=False)
            except BrowserManagerError:
                raise
            except (FileNotFoundError, OSError, ValueError) as exc:
                raise BrowserManagerError(
                    "browser_runtime_missing",
                    "approved Chromium executable is unavailable",
                ) from exc
            if (
                executable != identity.executable
                or not stat.S_ISREG(details.st_mode)
                or details.st_uid != os.geteuid()
                or details.st_mode & 0o022
                or not os.access(executable, os.X_OK)
            ):
                raise BrowserManagerError("browser_runtime_changed", "approved Chromium identity changed before launch")
            executable_descriptor = _open_pinned_lock(
                executable,
                os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            )
            pinned_details = os.fstat(executable_descriptor)
            if (
                not stat.S_ISREG(pinned_details.st_mode)
                or (pinned_details.st_dev, pinned_details.st_ino)
                != (installation.executable_device, installation.executable_inode)
            ):
                raise BrowserManagerError("browser_runtime_changed", "approved Chromium bytes changed before launch")
            pinned_executable = Path(f"/proc/{os.getpid()}/fd/{executable_descriptor}")
            if matching_browser_processes(profile):
                raise BrowserManagerError(
                    "browser_reconciliation_required",
                    "an unleased Chromium process already owns the profile",
                )

            with _playwright_driver_environment(profile, identity.control_executable):
                try:
                    from playwright.sync_api import sync_playwright
                except ImportError as exc:
                    raise BrowserManagerError("playwright_missing", "required Playwright package is not installed") from exc
                playwright = sync_playwright().start()
            control_pid, control_ticks = _playwright_control_process(playwright, identity.control_executable)
            record_control_process(control_pid, control_ticks)
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                executable_path=str(pinned_executable),
                headless=mode == BrowserMode.HEADLESS,
                chromium_sandbox=True,
                handle_sigint=False,
                handle_sigterm=False,
                handle_sighup=False,
                timeout=realm.launch_timeout_seconds * 1000,
                env=_browser_environment(profile),
            )
            os.close(executable_descriptor)
            executable_descriptor = None
            pid = _await_browser_pid(profile, identity.executable, realm.launch_timeout_seconds)
            if process_uses_forbidden_sandbox_flags(pid):
                raise BrowserManagerError(
                    "browser_sandbox_disabled",
                    "Chromium started with a forbidden sandbox-disabling flag",
                )
            start_ticks = process_start_ticks(pid)
            if start_ticks is None:
                raise BrowserManagerError("browser_process_identity_missing", "Chromium process identity is unavailable")
            page = context.pages[0] if context.pages else context.new_page()
            session = ManagedBrowserSession(
                lease_id=lease_id,
                realm=realm.id,
                landing_url=realm.landing_url,
                context=context,
                page=page,
            )
            return ProcessRuntimeHandle(
                context=context,
                playwright=playwright,
                profile=profile,
                pid=pid,
                process_start_ticks=start_ticks,
                session=session,
                identity=identity,
                control_pid=control_pid,
                control_process_start_ticks=control_ticks,
            )
        except BaseException as exc:
            if executable_descriptor is not None:
                os.close(executable_descriptor)
                executable_descriptor = None
            try:
                _cleanup_partial(playwright, context, profile, identity)
            except BrowserManagerError as cleanup_exc:
                raise cleanup_exc from exc
            if isinstance(exc, BrowserManagerError):
                raise
            if not isinstance(exc, Exception):
                raise
            raise BrowserManagerError("browser_launch_failed", "approved Chromium failed to start") from exc


def _browser_environment(profile: Path) -> dict[str, str]:
    allowed = {
        "DBUS_SESSION_BUS_ADDRESS",
        "DISPLAY",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "WAYLAND_DISPLAY",
        "XAUTHORITY",
        "XDG_RUNTIME_DIR",
    }
    environment = {
        name: value
        for name, value in os.environ.items()
        if name in allowed or name.startswith("LC_")
    }
    environment["HOME"] = str(profile)
    environment["PATH"] = "/usr/bin:/bin"
    return environment


_DRIVER_ENVIRONMENT_LOCK = threading.Lock()


@contextmanager
def _playwright_driver_environment(profile: Path, control_executable: Path) -> Iterator[None]:
    """Start Playwright's Node driver without inheriting ambient executable or loader controls."""

    safe_environment = _browser_environment(profile)
    safe_environment["PLAYWRIGHT_NODEJS_PATH"] = str(control_executable)
    with _DRIVER_ENVIRONMENT_LOCK:
        original = dict(os.environ)
        os.environ.clear()
        os.environ.update(safe_environment)
        try:
            yield
        finally:
            os.environ.clear()
            os.environ.update(original)


def _call_bounded(operation: Callable[[], Any], *, timeout_seconds: float) -> None:
    completed = threading.Event()
    error: list[BaseException] = []

    def invoke() -> None:
        try:
            operation()
        except BaseException as exc:
            error.append(exc)
        finally:
            completed.set()

    threading.Thread(target=invoke, daemon=True, name="dispatch-browser-cleanup").start()
    if not completed.wait(timeout_seconds):
        raise BrowserManagerError("browser_cleanup_timeout", "browser cleanup operation exceeded its deadline")
    if error:
        raise error[0]


def _raw_playwright_control_process(playwright: Any) -> tuple[int, int] | None:
    try:
        process = playwright._impl_obj._connection._transport._proc
        pid = int(process.pid)
    except (AttributeError, TypeError, ValueError):
        return None
    ticks = process_start_ticks(pid)
    return None if ticks is None else (pid, ticks)


def _playwright_control_process(playwright: Any, expected_executable: Path) -> tuple[int, int]:
    control = _raw_playwright_control_process(playwright)
    if control is None:
        raise BrowserManagerError("browser_control_identity_missing", "Playwright control process is unavailable")
    pid, ticks = control
    if process_executable(pid) != expected_executable:
        raise BrowserManagerError("browser_control_identity_mismatch", "Playwright control process is not approved")
    return pid, ticks


def _cleanup_partial(
    playwright: Any | None,
    context: Any | None,
    profile: Path,
    identity: BrowserRuntimeIdentity,
) -> None:
    control: tuple[int, int] | None = None
    unverified_control: tuple[int, int] | None = None
    if playwright is not None:
        try:
            control = _playwright_control_process(playwright, identity.control_executable)
        except BaseException:
            try:
                unverified_control = _raw_playwright_control_process(playwright)
            except BaseException:
                unverified_control = None
    for operation in (
        context.close if context is not None else None,
        playwright.stop if playwright is not None else None,
    ):
        if operation is None:
            continue
        try:
            _call_bounded(operation, timeout_seconds=2)
        except BaseException:
            pass
    for pid in matching_browser_processes(profile):
        ticks = process_start_ticks(pid)
        if ticks is None or process_executable(pid) != identity.executable:
            continue
        try:
            terminate_owned_process(pid, ticks, profile, identity.executable)
        except BaseException:
            pass
    if control is not None and process_start_ticks(control[0]) == control[1]:
        try:
            terminate_control_process(control[0], control[1], identity.control_executable)
        except BaseException:
            pass
    control_alive = control is not None and process_start_ticks(control[0]) == control[1]
    unverified_control_alive = (
        unverified_control is not None
        and process_start_ticks(unverified_control[0]) == unverified_control[1]
    )
    if matching_browser_processes(profile) or control_alive or unverified_control_alive:
        raise BrowserManagerError(
            "browser_cleanup_failed",
            "partially started browser process tree could not be cleaned up",
        )


def _await_browser_pid(profile: Path, expected_executable: Path, timeout_seconds: int) -> int:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        matches = matching_browser_processes(profile)
        if len(matches) == 1:
            if process_executable(matches[0]) != expected_executable:
                raise BrowserManagerError(
                    "browser_process_identity_mismatch",
                    "profile-owning process is not the approved Chromium executable",
                )
            return matches[0]
        if len(matches) > 1:
            raise BrowserManagerError(
                "browser_process_identity_ambiguous",
                "multiple Chromium parent processes claim the profile",
            )
        time.sleep(0.05)
    raise BrowserManagerError("browser_process_identity_missing", "Chromium process identity was not discovered")


def matching_browser_processes(profile: Path) -> list[int]:
    profile_marker = f"--user-data-dir={profile}".encode("utf-8")
    matches: list[int] = []
    proc = Path("/proc")
    for item in proc.iterdir():
        if not item.name.isdigit():
            continue
        try:
            command_line = (item / "cmdline").read_bytes()
        except OSError:
            continue
        marker_at = command_line.find(profile_marker)
        if marker_at < 0:
            continue
        marker_end = marker_at + len(profile_marker)
        before_ok = marker_at == 0 or command_line[marker_at - 1 : marker_at] in {b" ", b"\0"}
        after_ok = marker_end == len(command_line) or command_line[marker_end : marker_end + 1] in {b" ", b"\0"}
        if not before_ok or not after_ok:
            continue
        if b"--type=" in command_line or b"--remote-debugging-pipe" not in command_line:
            continue
        matches.append(int(item.name))
    return sorted(matches)


def process_executable(pid: int) -> Path | None:
    try:
        return Path(f"/proc/{pid}/exe").resolve(strict=True)
    except OSError:
        return None


def process_uses_forbidden_sandbox_flags(pid: int) -> bool:
    try:
        command_line = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return True
    forbidden = (
        b"--disable-gpu-sandbox",
        b"--disable-namespace-sandbox",
        b"--disable-seccomp-filter-sandbox",
        b"--disable-setuid-sandbox",
        b"--no-sandbox",
        b"--no-zygote",
        b"--single-process",
    )
    for flag in forbidden:
        start = 0
        while True:
            marker_at = command_line.find(flag, start)
            if marker_at < 0:
                break
            marker_end = marker_at + len(flag)
            before_ok = marker_at == 0 or command_line[marker_at - 1 : marker_at] in {b" ", b"\0"}
            after_ok = marker_end == len(command_line) or command_line[marker_end : marker_end + 1] in {b" ", b"\0"}
            if before_ok and after_ok:
                return True
            start = marker_end
    return False


def process_start_ticks(pid: int) -> int | None:
    identity = _process_identity(pid)
    return None if identity is None else identity[1]


def _process_identity(pid: int) -> tuple[int, int] | None:
    try:
        value = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    closing = value.rfind(")")
    if closing < 0:
        return None
    fields = value[closing + 2 :].split()
    if len(fields) <= 19:
        return None
    if fields[0] == "Z":
        return None
    try:
        return int(fields[1]), int(fields[19])
    except ValueError:
        return None


def _owned_process_tree(pid: int, expected_start_ticks: int) -> dict[int, int]:
    identities: dict[int, tuple[int, int]] = {}
    for item in Path("/proc").iterdir():
        if not item.name.isdigit():
            continue
        process_id = int(item.name)
        identity = _process_identity(process_id)
        if identity is not None:
            identities[process_id] = identity
    if identities.get(pid, (0, 0))[1] != expected_start_ticks:
        return {}
    owned = {pid: expected_start_ticks}
    pending = [pid]
    while pending:
        parent = pending.pop()
        for child, (child_parent, child_ticks) in identities.items():
            if child_parent == parent and child not in owned:
                owned[child] = child_ticks
                pending.append(child)
    return owned


def _remaining_processes(identities: dict[int, int]) -> dict[int, int]:
    return {
        pid: ticks
        for pid, ticks in identities.items()
        if process_start_ticks(pid) == ticks
    }


def _signal_processes(identities: dict[int, int], requested_signal: signal.Signals) -> None:
    for pid, ticks in identities.items():
        if process_start_ticks(pid) != ticks:
            continue
        try:
            os.kill(pid, requested_signal)
        except ProcessLookupError:
            continue


def _terminate_process_tree(pid: int, expected_start_ticks: int) -> bool:
    identities = _owned_process_tree(pid, expected_start_ticks)
    if not identities:
        raise BrowserManagerError("browser_process_identity_mismatch", "owned browser process tree is unavailable")
    _signal_processes(identities, signal.SIGTERM)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not _remaining_processes(identities):
            return True
        time.sleep(0.05)
    _signal_processes(_remaining_processes(identities), signal.SIGKILL)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if not _remaining_processes(identities):
            return True
        time.sleep(0.05)
    raise BrowserManagerError("browser_cleanup_failed", "owned browser process tree did not terminate")


def terminate_control_process(
    pid: int,
    expected_start_ticks: int,
    expected_executable: Path,
) -> bool:
    current = process_start_ticks(pid)
    if current is None:
        return False
    if current != expected_start_ticks or process_executable(pid) != expected_executable:
        raise BrowserManagerError("browser_control_identity_mismatch", "recorded control PID is not approved")
    return _terminate_process_tree(pid, expected_start_ticks)


def terminate_owned_process(
    pid: int,
    expected_start_ticks: int,
    profile: Path,
    expected_executable: Path,
) -> bool:
    current = process_start_ticks(pid)
    if current is None:
        return False
    if current != expected_start_ticks:
        raise BrowserManagerError("browser_process_identity_mismatch", "recorded PID belongs to another process")
    if pid not in matching_browser_processes(profile):
        raise BrowserManagerError("browser_process_identity_mismatch", "recorded process does not own the profile")
    if process_executable(pid) != expected_executable:
        raise BrowserManagerError("browser_process_identity_mismatch", "recorded process is not the approved executable")

    return _terminate_process_tree(pid, expected_start_ticks)
