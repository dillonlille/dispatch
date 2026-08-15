from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace
from typing import Any

import pytest
import dispatch_core.browser_manager.runtime as runtime_module

from dispatch_core.browser_manager import (
    BrowserLeaseRequest,
    BrowserManager,
    BrowserManagerError,
    BrowserMode,
    BrowserPurpose,
    BrowserRealm,
    LeaseState,
    ManagedBrowserSession,
    RealmRegistry,
)
from dispatch_core.browser_manager.runtime import (
    BrowserLayout,
    FileLock,
    LeaseLocks,
    PlaywrightRuntime,
    matching_browser_processes,
    process_start_ticks,
    terminate_owned_process,
)
from dispatch_core.browser_manager.runtime_authority import BrowserRuntimeIdentity
from dispatch_core.browser_manager.models import utc_now
from dispatch_core.browser_manager.store import LeaseStore
from dispatch_core.paths import DispatchPaths


CODE_ROOT = Path(__file__).resolve().parents[3]
TEST_IDENTITY = BrowserRuntimeIdentity(
    generation="test-only",
    executable=Path(sys.executable).resolve(),
    executable_sha256="0" * 64,
    control_executable=Path(sys.executable).resolve(),
    control_executable_sha256="0" * 64,
)


@dataclass
class FakeHandle:
    pid: int
    process_start_ticks: int
    session: ManagedBrowserSession
    identity: BrowserRuntimeIdentity = TEST_IDENTITY
    alive: bool = True
    closed: bool = False
    close_fails: bool = False

    def is_alive(self) -> bool:
        return self.alive

    def close(self) -> None:
        if self.close_fails:
            raise BrowserManagerError("browser_cleanup_failed", "synthetic cleanup failure")
        self.closed = True
        self.alive = False


class FakeRuntime:
    def __init__(
        self,
        *,
        fail_once: bool = False,
        identity: BrowserRuntimeIdentity = TEST_IDENTITY,
    ) -> None:
        self.fail_once = fail_once
        self._identity = identity
        self.handles: list[FakeHandle] = []

    @property
    def identity(self) -> BrowserRuntimeIdentity:
        return self._identity

    def start(
        self,
        *,
        lease_id: str,
        profile: Path,
        realm: BrowserRealm,
        mode: BrowserMode,
        record_control_process: Any,
    ) -> FakeHandle:
        if self.fail_once:
            self.fail_once = False
            raise BrowserManagerError("browser_launch_failed", "synthetic launch failure")
        handle = FakeHandle(
            pid=4000 + len(self.handles),
            process_start_ticks=100 + len(self.handles),
            identity=self._identity,
            session=ManagedBrowserSession(
                lease_id=lease_id,
                realm=realm.id,
                landing_url=realm.landing_url,
                page=object(),
                context=object(),
            ),
        )
        self.handles.append(handle)
        return handle


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def paths(tmp_path: Path) -> DispatchPaths:
    return DispatchPaths.from_environment(
        {"HOME": str(tmp_path / "home")},
        code_root=CODE_ROOT,
    )


def request(realm: str = "amazon-operations", plugin_id: str = "dcr") -> BrowserLeaseRequest:
    return BrowserLeaseRequest(
        plugin_id=plugin_id,
        plugin_release="release-1",
        realm=realm,
        purpose=BrowserPurpose.COLLECTION,
    )


def browser_manager_for_testing(
    core_paths: DispatchPaths,
    *,
    runtime: Any,
    realms: RealmRegistry | None = None,
    maximum_browsers: int = 2,
    clock: Any = utc_now,
    reconcile_on_start: bool = True,
) -> BrowserManager:
    """Build an isolated manager without shipping a production injection hook."""

    value = object.__new__(BrowserManager)
    layout = BrowserLayout.from_paths(core_paths)
    layout.prepare()
    object.__setattr__(value, "_BrowserManager__runtime", runtime)
    object.__setattr__(value, "_BrowserManager__realms", realms or RealmRegistry())
    object.__setattr__(value, "_BrowserManager__maximum_browsers", maximum_browsers)
    object.__setattr__(value, "_BrowserManager__clock", clock)
    object.__setattr__(value, "_BrowserManager__layout", layout)
    object.__setattr__(value, "_BrowserManager__store", LeaseStore(layout.database))
    value._active = {}
    value._guarded = {}
    if reconcile_on_start:
        value.reconcile()
    return value


def playwright_runtime_for_testing(launch_executable: Path, identity_executable: Path) -> PlaywrightRuntime:
    import playwright

    module = Path(playwright.__file__).resolve(strict=True)
    package_root = module.parent
    control_executable = (package_root / "driver" / "node").resolve(strict=True)
    driver_cli = (package_root / "driver" / "package" / "cli.js").resolve(strict=True)
    identity = BrowserRuntimeIdentity(
        generation="test-only-real-runtime",
        executable=identity_executable.resolve(strict=True),
        executable_sha256=hashlib.sha256(identity_executable.read_bytes()).hexdigest(),
        control_executable=control_executable,
        control_executable_sha256=hashlib.sha256(control_executable.read_bytes()).hexdigest(),
    )
    installation = SimpleNamespace(
        identity=identity,
        playwright_module=module,
        playwright_driver_cli=driver_cli,
    )

    class StaticAuthority:
        def load(self, *, full_tree: bool = False) -> Any:
            assert full_tree is True
            return installation

    value = object.__new__(PlaywrightRuntime)
    object.__setattr__(value, "_PlaywrightRuntime__authority", StaticAuthority())
    object.__setattr__(value, "_PlaywrightRuntime__installation", installation)
    object.__setattr__(value, "_PlaywrightRuntime__launch_executable", launch_executable.resolve(strict=True))
    return value


def test_lease_lifecycle_is_durable_private_and_bounded(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    manager = browser_manager_for_testing(paths(tmp_path), runtime=runtime, reconcile_on_start=False)
    assert not hasattr(manager, "runtime")
    assert not hasattr(manager, "realms")
    assert not hasattr(manager, "clock")
    assert not hasattr(manager, "__dict__")
    for name, replacement in (
        ("runtime", FakeRuntime()),
        ("realms", RealmRegistry()),
        ("clock", utc_now),
        ("layout", manager.layout),
        ("store", manager.store),
        ("maximum_browsers", 8),
    ):
        with pytest.raises(AttributeError):
            setattr(manager, name, replacement)

    managed = manager.acquire(request())
    assert managed.lease.state == LeaseState.READY
    assert managed.session.realm == "amazon-operations"
    assert managed.activate().state == LeaseState.ACTIVE
    assert managed.release().state == LeaseState.CLOSED
    assert runtime.handles[0].closed is True

    row = manager.status()[0]
    assert row["state"] == "closed"
    assert row["runtime_generation"] == "test-only"
    assert row["process_tracking"] == "none"
    assert "process_running" not in row
    assert "pid" not in row
    assert "profile" not in row
    assert "endpoint" not in row
    assert manager.layout.database.stat().st_mode & 0o777 == 0o600
    profile = manager.layout.profiles / "amazon-operations" / "dcr" / "default"
    assert profile.stat().st_mode & 0o777 == 0o700


def test_realm_and_global_locks_allow_isolated_realms_only(tmp_path: Path) -> None:
    manager = browser_manager_for_testing(paths(tmp_path), runtime=FakeRuntime(), reconcile_on_start=False)
    amazon = manager.acquire(request("amazon-operations", "dcr"))
    with pytest.raises(BrowserManagerError) as denied:
        manager.acquire(request("amazon-operations", "scorecard"))
    assert denied.value.code == "browser_realm_busy"

    paycom = manager.acquire(request("paycom-client", "timecard"))
    assert paycom.lease.state == LeaseState.READY
    amazon.release()
    paycom.release()


def test_failed_launch_releases_locks_and_records_terminal_failure(tmp_path: Path) -> None:
    runtime = FakeRuntime(fail_once=True)
    manager = browser_manager_for_testing(paths(tmp_path), runtime=runtime, reconcile_on_start=False)

    with pytest.raises(BrowserManagerError) as failed:
        manager.acquire(request())
    assert failed.value.code == "browser_launch_failed"
    assert manager.status()[0]["state"] == "failed"
    assert manager.status()[0]["error_code"] == "browser_launch_failed"

    recovered = manager.acquire(request())
    assert recovered.lease.state == LeaseState.READY
    recovered.release()


def test_maintenance_fails_crashed_and_expired_leases(tmp_path: Path) -> None:
    clock = MutableClock()
    runtime = FakeRuntime()
    realm = BrowserRealm(
        id="test-realm",
        landing_url="https://example.invalid/landing",
        purposes=frozenset({BrowserPurpose.COLLECTION}),
        lease_timeout_seconds=30,
    )
    manager = browser_manager_for_testing(
        paths(tmp_path),
        runtime=runtime,
        realms=RealmRegistry([realm]),
        clock=clock,
        reconcile_on_start=False,
    )

    crashed = manager.acquire(request("test-realm", "collector-one"))
    runtime.handles[0].alive = False
    assert manager.maintain() == [{"lease_id": crashed.lease_id, "status": "browser_crashed"}]
    assert crashed.lease.state == LeaseState.FAILED

    expiring = manager.acquire(request("test-realm", "collector-two"))
    clock.advance(31)
    assert manager.maintain() == [{"lease_id": expiring.lease_id, "status": "browser_lease_expired"}]
    assert expiring.lease.state == LeaseState.FAILED
    assert runtime.handles[1].closed is True


def test_interrupted_row_without_a_process_is_reconciled(tmp_path: Path) -> None:
    core_paths = paths(tmp_path)
    first = browser_manager_for_testing(core_paths, runtime=FakeRuntime(), reconcile_on_start=False)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    created = first.store.create(
        lease_id="a" * 32,
        request=request(),
        mode=BrowserMode.HEADLESS,
        created_at=now,
        expires_at=now + timedelta(minutes=15),
        runtime_identity=TEST_IDENTITY,
        maximum_browsers=2,
    )
    first.store.transition(created.lease_id, LeaseState.STARTING, now)

    second = browser_manager_for_testing(core_paths, runtime=FakeRuntime(), reconcile_on_start=True)
    row = second.store.get(created.lease_id)
    assert row.state == LeaseState.QUARANTINED
    assert row.error_code == "browser_launch_identity_pending"


def test_reconciliation_quarantines_a_lease_from_another_runtime_generation(tmp_path: Path) -> None:
    core_paths = paths(tmp_path)
    other_identity = BrowserRuntimeIdentity(
        generation="other-generation",
        executable=Path(sys.executable).resolve(),
        executable_sha256="1" * 64,
        control_executable=Path(sys.executable).resolve(),
        control_executable_sha256="1" * 64,
    )
    first = browser_manager_for_testing(
        core_paths,
        runtime=FakeRuntime(identity=other_identity),
        reconcile_on_start=False,
    )
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    created = first.store.create(
        lease_id="c" * 32,
        request=request(),
        mode=BrowserMode.HEADLESS,
        created_at=now,
        expires_at=now + timedelta(minutes=15),
        runtime_identity=other_identity,
        maximum_browsers=2,
    )
    first.store.transition(created.lease_id, LeaseState.STARTING, now)

    second = browser_manager_for_testing(core_paths, runtime=FakeRuntime(), reconcile_on_start=True)
    row = second.store.get(created.lease_id)

    assert row.state == LeaseState.QUARANTINED
    assert row.error_code == "browser_runtime_identity_mismatch"


def test_cleanup_failure_quarantines_profile_until_reconciliation(tmp_path: Path) -> None:
    core_paths = paths(tmp_path)
    runtime = FakeRuntime()
    manager = browser_manager_for_testing(core_paths, runtime=runtime, maximum_browsers=1, reconcile_on_start=False)
    managed = manager.acquire(request())
    runtime.handles[0].close_fails = True

    assert managed.release().state == LeaseState.QUARANTINED
    with pytest.raises(BrowserManagerError) as blocked:
        manager.acquire(request())
    assert blocked.value.code == "browser_profile_busy"
    with pytest.raises(BrowserManagerError) as realm_blocked:
        manager.acquire(request("amazon-operations", "scorecard"))
    assert realm_blocked.value.code == "browser_realm_busy"
    with pytest.raises(BrowserManagerError) as capacity_blocked:
        manager.acquire(request("paycom-client", "timecard"))
    assert capacity_blocked.value.code == "browser_capacity_unavailable"

    assert manager.maintain() == [{"lease_id": managed.lease_id, "status": "process_absent"}]
    assert manager.store.get(managed.lease_id).state == LeaseState.FAILED


def test_pidless_interrupted_launch_terminates_matching_orphan(tmp_path: Path) -> None:
    core_paths = paths(tmp_path)
    first = browser_manager_for_testing(core_paths, runtime=FakeRuntime(), reconcile_on_start=False)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    created = first.store.create(
        lease_id="b" * 32,
        request=request(),
        mode=BrowserMode.HEADLESS,
        created_at=now,
        expires_at=now + timedelta(minutes=15),
        runtime_identity=TEST_IDENTITY,
        maximum_browsers=2,
    )
    first.store.transition(created.lease_id, LeaseState.STARTING, now)
    profile = first.layout.profile("amazon-operations", "dcr", "default")
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            f"--user-data-dir={profile}",
            "--remote-debugging-pipe",
        ],
        start_new_session=True,
    )
    try:
        for _ in range(100):
            if process.pid in matching_browser_processes(profile):
                break
            time.sleep(0.01)
        assert process.pid in matching_browser_processes(profile)
        second = browser_manager_for_testing(core_paths, runtime=FakeRuntime(), reconcile_on_start=True)
        row = second.store.get(created.lease_id)
        assert row.state == LeaseState.FAILED
        assert row.error_code == "orphan_terminated"
        assert process.wait(timeout=5) is not None
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def test_pidless_launch_remains_quarantined_until_a_late_process_is_reconciled(tmp_path: Path) -> None:
    core_paths = paths(tmp_path)
    first = browser_manager_for_testing(core_paths, runtime=FakeRuntime(), reconcile_on_start=False)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    created = first.store.create(
        lease_id="c" * 32,
        request=request(),
        mode=BrowserMode.HEADLESS,
        created_at=now,
        expires_at=now + timedelta(minutes=15),
        runtime_identity=TEST_IDENTITY,
        maximum_browsers=2,
    )
    first.store.transition(created.lease_id, LeaseState.STARTING, now)
    second = browser_manager_for_testing(core_paths, runtime=FakeRuntime(), reconcile_on_start=True)
    row = second.store.get(created.lease_id)
    assert row.state == LeaseState.QUARANTINED
    assert row.error_code == "browser_launch_identity_pending"

    profile = second.layout.profile("amazon-operations", "dcr", "default")
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            f"--user-data-dir={profile}",
            "--remote-debugging-pipe",
        ],
        start_new_session=True,
    )
    try:
        for _ in range(100):
            if process.pid in matching_browser_processes(profile):
                break
            time.sleep(0.01)
        assert second.maintain() == [{"lease_id": created.lease_id, "status": "orphan_terminated"}]
        assert second.store.get(created.lease_id).state == LeaseState.FAILED
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def test_close_state_error_retains_process_ownership_and_locks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core_paths = paths(tmp_path)
    manager = browser_manager_for_testing(core_paths, runtime=FakeRuntime(), reconcile_on_start=False)
    managed = manager.acquire(request())
    original_transition = manager.store.transition

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        if args[1] == LeaseState.CLOSING:
            raise sqlite3.OperationalError("synthetic unavailable state store")
        return original_transition(*args, **kwargs)

    monkeypatch.setattr(manager.store, "transition", unavailable)
    with pytest.raises(sqlite3.OperationalError):
        managed.release()
    assert managed.lease_id in manager._active

    other = browser_manager_for_testing(core_paths, runtime=FakeRuntime(), reconcile_on_start=False)
    with pytest.raises(BrowserManagerError) as blocked:
        other.acquire(request())
    assert blocked.value.code in {"browser_realm_busy", "browser_capacity_unavailable"}

    monkeypatch.setattr(manager.store, "transition", original_transition)
    assert manager.maintain() == [
        {"lease_id": managed.lease_id, "status": "browser_cleanup_retry"}
    ]
    assert managed.lease.state == LeaseState.CLOSED


def test_failed_launch_state_error_releases_safe_locks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = browser_manager_for_testing(paths(tmp_path), runtime=FakeRuntime(fail_once=True), reconcile_on_start=False)

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise sqlite3.OperationalError("synthetic unavailable state store")

    monkeypatch.setattr(BrowserManager, "_fail_stored", unavailable)
    with pytest.raises(BrowserManagerError) as failed:
        manager.acquire(request())
    assert failed.value.code == "browser_state_unavailable"

    row = manager.store.nonterminal()[0]
    locks = LeaseLocks.acquire(
        manager.layout,
        maximum_browsers=manager.maximum_browsers,
        realm=row.realm,
        profile_key=row.profile_key,
    )
    locks.release()


def test_partial_runtime_cleanup_failure_is_not_suppressed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setattr(runtime_module, "matching_browser_processes", lambda unused: [4242])
    monkeypatch.setattr(runtime_module, "process_start_ticks", lambda unused: 99)
    monkeypatch.setattr(runtime_module, "process_executable", lambda unused: TEST_IDENTITY.executable)

    def cleanup_fails(pid: int, ticks: int, path: Path, executable: Path) -> bool:
        raise BrowserManagerError("browser_cleanup_failed", "synthetic partial cleanup failure")

    monkeypatch.setattr(runtime_module, "terminate_owned_process", cleanup_fails)
    with pytest.raises(BrowserManagerError) as failed:
        runtime_module._cleanup_partial(None, None, profile, TEST_IDENTITY)
    assert failed.value.code == "browser_cleanup_failed"


def test_startup_cleanup_failure_quarantines_global_capacity(tmp_path: Path) -> None:
    class PartialCleanupFailureRuntime:
        @property
        def identity(self) -> BrowserRuntimeIdentity:
            return TEST_IDENTITY

        def start(self, **kwargs: Any) -> FakeHandle:
            raise BrowserManagerError("browser_cleanup_failed", "synthetic partial cleanup failure")

    manager = browser_manager_for_testing(
        paths(tmp_path),
        runtime=PartialCleanupFailureRuntime(),
        maximum_browsers=1,
        reconcile_on_start=False,
    )
    with pytest.raises(BrowserManagerError) as failed:
        manager.acquire(request())
    assert failed.value.code == "browser_cleanup_failed"
    assert manager.status()[0]["state"] == "quarantined"
    with pytest.raises(BrowserManagerError) as blocked:
        manager.acquire(request("paycom-client", "timecard"))
    assert blocked.value.code == "browser_capacity_unavailable"


def test_lock_rejects_hard_link_without_modifying_target(tmp_path: Path) -> None:
    manager = browser_manager_for_testing(paths(tmp_path), runtime=FakeRuntime(), reconcile_on_start=False)
    target = manager.layout.locks / "protected"
    target.write_text("preserve\n", encoding="utf-8")
    target.chmod(0o640)
    lock_path = manager.layout.lock_path("profile", "hard-link-test")
    os.link(target, lock_path)

    with pytest.raises(BrowserManagerError) as denied:
        FileLock(lock_path, "browser_profile_busy").acquire()
    assert denied.value.code == "unsafe_browser_storage"
    assert target.read_text(encoding="utf-8") == "preserve\n"
    assert target.stat().st_mode & 0o777 == 0o640


def test_unknown_schema_version_fails_closed(tmp_path: Path) -> None:
    manager = browser_manager_for_testing(paths(tmp_path), runtime=FakeRuntime(), reconcile_on_start=False)
    with sqlite3.connect(manager.layout.database) as connection:
        connection.execute("UPDATE metadata SET value = '999' WHERE key = 'schema_version'")
        connection.commit()

    with pytest.raises(BrowserManagerError) as rejected:
        LeaseStore(manager.layout.database)
    assert rejected.value.code == "unsupported_browser_schema"


def test_unapproved_schema_columns_fail_closed(tmp_path: Path) -> None:
    manager = browser_manager_for_testing(paths(tmp_path), runtime=FakeRuntime(), reconcile_on_start=False)
    with sqlite3.connect(manager.layout.database) as connection:
        connection.execute("ALTER TABLE leases ADD COLUMN unapproved TEXT")
        connection.commit()

    with pytest.raises(BrowserManagerError) as rejected:
        LeaseStore(manager.layout.database)
    assert rejected.value.code == "browser_state_corrupt"


def test_owned_process_tree_cleanup_terminates_descendants(tmp_path: Path) -> None:
    manager = browser_manager_for_testing(paths(tmp_path), runtime=FakeRuntime(), reconcile_on_start=False)
    profile = manager.layout.profile("amazon-operations", "dcr", "default")
    child_pid_file = tmp_path / "child-pid"
    script = (
        "import pathlib, subprocess, sys, time; "
        "child=subprocess.Popen(['sleep','60']); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); "
        "time.sleep(60)"
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(child_pid_file),
            f"--user-data-dir={profile}",
            "--remote-debugging-pipe",
        ],
        start_new_session=True,
    )
    try:
        for _ in range(100):
            if child_pid_file.exists():
                break
            time.sleep(0.01)
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))
        ticks = process_start_ticks(process.pid)
        assert ticks is not None
        assert terminate_owned_process(process.pid, ticks, profile, Path(sys.executable).resolve()) is True
        process.wait(timeout=5)
        assert process_start_ticks(child_pid) is None
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def test_process_cleanup_refuses_a_profile_match_from_another_executable(tmp_path: Path) -> None:
    manager = browser_manager_for_testing(paths(tmp_path), runtime=FakeRuntime(), reconcile_on_start=False)
    profile = manager.layout.profile("amazon-operations", "dcr", "default")
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            f"--user-data-dir={profile}",
            "--remote-debugging-pipe",
        ],
        start_new_session=True,
    )
    try:
        ticks = process_start_ticks(process.pid)
        assert ticks is not None
        with pytest.raises(BrowserManagerError) as rejected:
            terminate_owned_process(process.pid, ticks, profile, Path("/bin/false").resolve())
        assert rejected.value.code == "browser_process_identity_mismatch"
        assert process.poll() is None
        assert terminate_owned_process(process.pid, ticks, profile, Path(sys.executable).resolve()) is True
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def test_browser_environment_does_not_forward_browser_authority_overrides(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/tmp/untrusted-playwright")
    monkeypatch.setenv("AGENT_BROWSER_EXECUTABLE_PATH", "/tmp/untrusted-chromium")
    monkeypatch.setenv("HOME", "/tmp/untrusted-home")
    monkeypatch.setenv("PATH", "/tmp/untrusted-path")
    profile = tmp_path / "private-profile"

    environment = runtime_module._browser_environment(profile)

    assert "PLAYWRIGHT_BROWSERS_PATH" not in environment
    assert "AGENT_BROWSER_EXECUTABLE_PATH" not in environment
    assert environment["HOME"] == str(profile)
    assert environment["PATH"] == "/usr/bin:/bin"


def test_playwright_driver_startup_environment_is_scrubbed_and_restored(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("LD_PRELOAD", "/tmp/untrusted-loader.so")
    monkeypatch.setenv("NODE_OPTIONS", "--require=/tmp/untrusted.js")
    monkeypatch.setenv("PLAYWRIGHT_NODEJS_PATH", "/tmp/untrusted-node")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/tmp/untrusted-browsers")
    profile = tmp_path / "private-profile"
    control = Path(sys.executable).resolve()

    with runtime_module._playwright_driver_environment(profile, control):
        assert os.environ["PLAYWRIGHT_NODEJS_PATH"] == str(control)
        assert os.environ["HOME"] == str(profile)
        assert os.environ["PATH"] == "/usr/bin:/bin"
        assert "LD_PRELOAD" not in os.environ
        assert "NODE_OPTIONS" not in os.environ
        assert "PLAYWRIGHT_BROWSERS_PATH" not in os.environ

    assert os.environ["LD_PRELOAD"] == "/tmp/untrusted-loader.so"
    assert os.environ["NODE_OPTIONS"] == "--require=/tmp/untrusted.js"
    assert os.environ["PLAYWRIGHT_NODEJS_PATH"] == "/tmp/untrusted-node"


def test_cleanup_calls_are_bounded() -> None:
    started = time.monotonic()
    with pytest.raises(BrowserManagerError) as rejected:
        runtime_module._call_bounded(lambda: time.sleep(60), timeout_seconds=0.05)
    assert rejected.value.code == "browser_cleanup_timeout"
    assert time.monotonic() - started < 1


def test_partial_cleanup_quarantines_an_unverified_live_control_process(tmp_path: Path) -> None:
    process = subprocess.Popen(["/bin/sleep", "30"])
    playwright = SimpleNamespace(
        _impl_obj=SimpleNamespace(
            _connection=SimpleNamespace(
                _transport=SimpleNamespace(_proc=process),
            )
        ),
        stop=lambda: None,
    )
    try:
        with pytest.raises(BrowserManagerError) as rejected:
            runtime_module._cleanup_partial(playwright, None, tmp_path / "profile", TEST_IDENTITY)
        assert rejected.value.code == "browser_cleanup_failed"
        assert process.poll() is None
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


@pytest.mark.skipif(os.environ.get("DISPATCH_BROWSER_INTEGRATION") != "1", reason="explicit real-browser test")
def test_playwright_runtime_rejects_no_sandbox_chromium(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import playwright

    manifest = json.loads(
        (Path(playwright.__file__).parent / "driver" / "package" / "browsers.json").read_text(encoding="utf-8")
    )
    revision = next(item["revision"] for item in manifest["browsers"] if item["name"] == "chromium")
    cache = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", Path.home() / ".cache" / "ms-playwright"))
    chromium = (cache / f"chromium-{revision}" / "chrome-linux64" / "chrome").resolve(strict=True)
    wrapper = tmp_path / "test-chromium"
    wrapper.write_text(
        f"#!/bin/sh\nexec {shlex.quote(str(chromium))} --no-sandbox \"$@\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o700)
    injection_marker = tmp_path / "node-options-executed"
    injection = tmp_path / "untrusted-node-options.js"
    injection.write_text(
        f"require('fs').writeFileSync({json.dumps(str(injection_marker))}, 'executed');\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("NODE_OPTIONS", f"--require={injection}")
    monkeypatch.setenv("PLAYWRIGHT_NODEJS_PATH", "/bin/false")

    realm = BrowserRealm(
        id="test-realm",
        landing_url="https://example.invalid/landing",
        purposes=frozenset({BrowserPurpose.HEALTHCHECK}),
        lease_timeout_seconds=60,
    )
    manager = browser_manager_for_testing(
        paths(tmp_path),
        runtime=playwright_runtime_for_testing(wrapper, chromium),
        realms=RealmRegistry([realm]),
        reconcile_on_start=False,
    )
    with pytest.raises(BrowserManagerError) as rejected:
        manager.acquire(
            BrowserLeaseRequest(
                plugin_id="browser-doctor",
                plugin_release="release-1",
                realm="test-realm",
                purpose=BrowserPurpose.HEALTHCHECK,
            )
        )
    assert rejected.value.code == "browser_sandbox_disabled"
    row = manager.store.recent(1)[0]
    assert row.control_pid is not None
    assert row.control_process_start_ticks is not None
    assert process_start_ticks(row.control_pid) is None
    assert not injection_marker.exists()
    assert matching_browser_processes(manager.layout.profile("test-realm", "browser-doctor", "default")) == []
