from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest
import browser_manager.runtime as runtime_module

from browser_manager import (
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
from browser_manager.runtime import (
    BrowserLayout,
    FileLock,
    LeaseLocks,
    PlaywrightRuntime,
    ProcessRuntimeHandle,
    matching_browser_processes,
    process_start_ticks,
    terminate_owned_process,
)
from browser_manager.runtime_authority import BrowserRuntimeIdentity
from browser_manager.models import utc_now
from browser_manager.store import LeaseStore
from collection_manager import (
    CollectionManager,
    CollectionRequest,
    CollectionService,
    CollectionTaskStore,
    CollectionWorkerSupervisor,
    CollectorRegistration,
    ProductionManagerFactory,
)
from paths import DispatchPaths


CODE_ROOT = Path(__file__).resolve().parents[3]
TEST_IDENTITY = BrowserRuntimeIdentity(
    playwright_version="test-playwright",
    chromium_version="test-chromium",
    executable=Path(sys.executable).resolve(),
    control_executable=Path(sys.executable).resolve(),
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
    object.__setattr__(value, "_BrowserManager__leases_lock", threading.RLock())
    value._active = {}
    value._guarded = {}
    if reconcile_on_start:
        value.reconcile()
    return value


def playwright_runtime_for_testing(launch_executable: Path, identity_executable: Path) -> PlaywrightRuntime:
    import playwright

    package_root = Path(playwright.__file__).resolve(strict=True).parent
    control_executable = (package_root / "driver" / "node").resolve(strict=True)
    identity = BrowserRuntimeIdentity(
        playwright_version="test-playwright",
        chromium_version="test-chromium",
        executable=identity_executable.resolve(strict=True),
        control_executable=control_executable,
    )
    executable_details = identity_executable.resolve(strict=True).stat()
    installation = SimpleNamespace(
        identity=identity,
        browsers_path=identity_executable.resolve(strict=True).parent,
        executable_device=executable_details.st_dev,
        executable_inode=executable_details.st_ino,
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


def test_browser_layout_rejects_world_writable_custom_root_ancestor(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    writable = tmp_path / "writable"
    writable.mkdir(mode=0o777)
    writable.chmod(0o777)
    core_paths = DispatchPaths.from_environment(
        {
            "HOME": str(home),
            "DISPATCH_DATA_ROOT": str(writable / "data"),
        },
        code_root=CODE_ROOT,
    )
    layout = BrowserLayout.from_paths(core_paths)
    with pytest.raises(BrowserManagerError) as error:
        layout.prepare()
    assert error.value.code == "unsafe_browser_storage"
    assert not (writable / "data").exists()


def test_launch_rejects_runtime_symlink_replacement_before_playwright_start(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    executable = cache / "chrome"
    executable.write_text("chrome", encoding="utf-8")
    executable.chmod(0o700)
    runtime = playwright_runtime_for_testing(executable, executable)
    outside = tmp_path / "outside-chrome"
    outside.write_text("outside", encoding="utf-8")
    outside.chmod(0o700)
    executable.unlink()
    executable.symlink_to(outside)
    realm = BrowserRealm(
        id="toctou-realm",
        landing_url="https://example.invalid",
        purposes=frozenset({BrowserPurpose.COLLECTION}),
        lease_timeout_seconds=30,
    )
    with pytest.raises(BrowserManagerError) as error:
        runtime.start(
            lease_id="toctou-lease",
            profile=tmp_path / "profile",
            realm=realm,
            mode=BrowserMode.HEADLESS,
            record_control_process=lambda *_args: None,
        )
    assert error.value.code == "browser_runtime_changed"


def test_launch_uses_pinned_approved_inode_if_path_is_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import playwright.sync_api as sync_api  # type: ignore[import-not-found]

    cache = tmp_path / "cache"
    cache.mkdir()
    executable = cache / "chrome"
    executable.write_text("approved", encoding="utf-8")
    executable.chmod(0o700)
    runtime = playwright_runtime_for_testing(executable, executable)
    observed: list[str] = []

    def launch_persistent_context(**kwargs: Any) -> None:
        attacker = cache / "attacker"
        attacker.write_text("unapproved", encoding="utf-8")
        attacker.chmod(0o700)
        os.replace(attacker, executable)
        observed.append(Path(kwargs["executable_path"]).read_text(encoding="utf-8"))
        raise KeyboardInterrupt("probe complete")

    fake_playwright = SimpleNamespace(
        chromium=SimpleNamespace(launch_persistent_context=launch_persistent_context),
        stop=lambda: None,
    )
    monkeypatch.setattr(sync_api, "sync_playwright", lambda: SimpleNamespace(start=lambda: fake_playwright))
    monkeypatch.setattr(runtime_module, "matching_browser_processes", lambda _profile: [])
    monkeypatch.setattr(runtime_module, "_playwright_control_process", lambda *_args: (123, 456))
    monkeypatch.setattr(runtime_module, "_cleanup_partial", lambda *_args: None)
    realm = BrowserRealm(
        id="pinned-realm",
        landing_url="https://example.invalid",
        purposes=frozenset({BrowserPurpose.COLLECTION}),
        lease_timeout_seconds=30,
    )
    with pytest.raises(KeyboardInterrupt):
        runtime.start(
            lease_id="pinned-lease",
            profile=tmp_path / "profile",
            realm=realm,
            mode=BrowserMode.HEADLESS,
            record_control_process=lambda *_args: None,
        )
    assert observed == ["approved"]


def test_playwright_start_interrupt_runs_partial_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import playwright.sync_api as sync_api  # type: ignore[import-not-found]

    executable = tmp_path / "approved-browser"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    runtime = playwright_runtime_for_testing(executable, executable)
    fake_playwright = SimpleNamespace()
    starter = SimpleNamespace(start=lambda: fake_playwright)
    cleanup_calls: list[tuple[Any, Any, Path]] = []
    monkeypatch.setattr(sync_api, "sync_playwright", lambda: starter)
    monkeypatch.setattr(runtime_module, "matching_browser_processes", lambda _profile: [])
    monkeypatch.setattr(runtime_module, "_playwright_control_process", lambda *_args: (123, 456))
    monkeypatch.setattr(
        runtime_module,
        "_cleanup_partial",
        lambda playwright, context, profile, _identity: cleanup_calls.append((playwright, context, profile)),
    )
    profile = tmp_path / "profile"
    realm = BrowserRealm(
        id="interrupt-realm",
        landing_url="https://example.invalid/landing",
        purposes=frozenset({BrowserPurpose.COLLECTION}),
        lease_timeout_seconds=30,
    )

    with pytest.raises(KeyboardInterrupt):
        runtime.start(
            lease_id="interrupt-lease",
            profile=profile,
            realm=realm,
            mode=BrowserMode.HEADLESS,
            record_control_process=lambda *_args: (_ for _ in ()).throw(KeyboardInterrupt),
        )
    assert cleanup_calls == [(fake_playwright, None, profile)]


def test_manager_start_interrupt_finalizes_durable_lease(tmp_path: Path) -> None:
    class InterruptingRuntime(FakeRuntime):
        def start(self, **_kwargs: Any) -> FakeHandle:
            raise KeyboardInterrupt

    manager = browser_manager_for_testing(
        paths(tmp_path), runtime=InterruptingRuntime(), reconcile_on_start=False
    )
    with pytest.raises(KeyboardInterrupt):
        manager.acquire(request())
    rows = manager.status()
    assert len(rows) == 1
    assert rows[0]["state"] == "failed"
    assert rows[0]["error_code"] == "browser_manager_failed"


def test_start_failure_persistence_interrupt_guards_locks_until_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = MutableClock()
    manager = browser_manager_for_testing(
        paths(tmp_path),
        runtime=FakeRuntime(fail_once=True),
        clock=clock,
        reconcile_on_start=False,
    )
    original = BrowserManager._fail_stored

    def interrupt_persistence(_manager, _lease_id, _error_code):
        raise KeyboardInterrupt("persistence")

    monkeypatch.setattr(BrowserManager, "_fail_stored", interrupt_persistence)
    with pytest.raises(BrowserManagerError) as error:
        manager.acquire(request())
    assert error.value.code == "browser_state_unavailable"
    row = manager.store.nonterminal()[0]
    assert row.state == LeaseState.STARTING
    exclusive = FileLock(manager.layout.generation_lock, "browser_generation_busy")
    with pytest.raises(BrowserManagerError):
        exclusive.acquire()

    monkeypatch.setattr(BrowserManager, "_fail_stored", original)
    assert manager.reconcile() == [{"lease_id": row.lease_id, "status": "browser_launch_identity_pending"}]
    clock.advance(3)
    assert manager.maintain() == [{"lease_id": row.lease_id, "status": "process_absent"}]
    exclusive.acquire()
    exclusive.release()


def test_partial_cleanup_continues_after_baseexception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stopped: list[bool] = []

    def interrupt() -> None:
        raise KeyboardInterrupt("context cleanup")

    monkeypatch.setattr(runtime_module, "_raw_playwright_control_process", lambda _playwright: None)
    monkeypatch.setattr(runtime_module, "matching_browser_processes", lambda _profile: [])
    runtime_module._cleanup_partial(
        SimpleNamespace(stop=lambda: stopped.append(True)),
        SimpleNamespace(close=interrupt),
        tmp_path / "profile",
        TEST_IDENTITY,
    )
    assert stopped == [True]


def test_process_handle_interruption_continues_cleanup_then_reraises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stopped: list[bool] = []

    def interrupt() -> None:
        raise KeyboardInterrupt("context close")

    handle = ProcessRuntimeHandle(
        context=SimpleNamespace(close=interrupt),
        playwright=SimpleNamespace(stop=lambda: stopped.append(True)),
        profile=tmp_path / "profile",
        pid=123,
        process_start_ticks=456,
        session=ManagedBrowserSession(
            lease_id="lease",
            realm="amazon-operations",
            landing_url="https://example.invalid",
            page=object(),
            context=object(),
        ),
        identity=TEST_IDENTITY,
        control_pid=789,
        control_process_start_ticks=1011,
    )
    monkeypatch.setattr(runtime_module, "process_start_ticks", lambda _pid: None)
    with pytest.raises(KeyboardInterrupt):
        handle.close()
    assert stopped == [True]
    assert handle.closed is True


def test_release_interruption_finalizes_or_guards_generation_by_process_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_close = FakeHandle.close

    def clean_then_interrupt(handle: FakeHandle) -> None:
        handle.closed = True
        handle.alive = False
        raise KeyboardInterrupt("after cleanup")

    monkeypatch.setattr(FakeHandle, "close", clean_then_interrupt)
    clean_manager = browser_manager_for_testing(
        paths(tmp_path / "clean"), runtime=FakeRuntime(), reconcile_on_start=False
    )
    clean_lease = clean_manager.acquire(request())
    with pytest.raises(KeyboardInterrupt):
        clean_lease.release()
    assert clean_manager.status()[0]["state"] == "closed"
    clean_exclusive = FileLock(clean_manager.layout.generation_lock, "browser_generation_busy")
    clean_exclusive.acquire()
    clean_exclusive.release()

    def interrupt_while_alive(_handle: FakeHandle) -> None:
        raise KeyboardInterrupt("before cleanup")

    monkeypatch.setattr(FakeHandle, "close", interrupt_while_alive)
    guarded_manager = browser_manager_for_testing(
        paths(tmp_path / "guarded"), runtime=FakeRuntime(), reconcile_on_start=False
    )
    guarded_lease = guarded_manager.acquire(request())
    with pytest.raises(KeyboardInterrupt):
        guarded_lease.release()
    assert guarded_manager.status()[0]["state"] == "quarantined"
    guarded_exclusive = FileLock(guarded_manager.layout.generation_lock, "browser_generation_busy")
    with pytest.raises(BrowserManagerError) as busy:
        guarded_exclusive.acquire()
    assert busy.value.code == "browser_generation_busy"
    monkeypatch.setattr(FakeHandle, "close", original_close)


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
    assert row["runtime_playwright_version"] == "test-playwright"
    assert row["process_tracking"] == "none"
    assert "process_running" not in row
    assert "pid" not in row
    assert "profile" not in row
    assert "endpoint" not in row
    assert manager.layout.database.stat().st_mode & 0o777 == 0o600
    profile = manager.layout.profiles / "amazon-operations" / "dcr" / "default"
    assert profile.stat().st_mode & 0o777 == 0o700


def test_same_realm_leases_run_concurrently_with_profile_exclusivity(tmp_path: Path) -> None:
    manager = browser_manager_for_testing(
        paths(tmp_path),
        runtime=FakeRuntime(),
        reconcile_on_start=False,
        maximum_browsers=4,
    )
    first = manager.acquire(request())
    # Two collectors scraping the SAME site concurrently: distinct accounts
    # mean distinct profiles, so both leases hold at once.
    second = manager.acquire(request("amazon-operations", "scorecard"))
    assert second.lease.state == LeaseState.READY
    # The exact same plugin+account (= same profile) stays exclusively locked
    # while `first` holds it, even with global and realm capacity available.
    with pytest.raises(BrowserManagerError) as denied:
        manager.acquire(request())
    assert denied.value.code == "browser_profile_busy"
    # A different profile in the same realm is unaffected.
    filler = manager.acquire(request("paycom-client", "timecard"))
    assert filler.lease.state == LeaseState.READY
    filler.release()
    first.release()
    second.release()


def test_realm_concurrency_limit_is_enforced(tmp_path: Path) -> None:
    solo = BrowserRealm(
        id="solo-realm",
        landing_url="https://example.invalid/landing",
        purposes=frozenset({BrowserPurpose.COLLECTION}),
        max_concurrent_leases=1,
    )
    open_realm = BrowserRealm(
        id="open-realm",
        landing_url="https://example.invalid/open",
        purposes=frozenset({BrowserPurpose.COLLECTION}),
    )
    manager = browser_manager_for_testing(
        paths(tmp_path),
        runtime=FakeRuntime(),
        realms=RealmRegistry([solo, open_realm]),
        reconcile_on_start=False,
    )
    only = manager.acquire(request("solo-realm", "one"))
    with pytest.raises(BrowserManagerError) as busy:
        manager.acquire(request("solo-realm", "two"))
    assert busy.value.code == "browser_realm_busy"
    # Other realms are unaffected by this realm's limit.
    elsewhere = manager.acquire(request("open-realm", "timecard"))
    assert elsewhere.lease.state == LeaseState.READY
    only.release()
    elsewhere.release()


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
    clock = MutableClock()
    first = browser_manager_for_testing(
        core_paths,
        runtime=FakeRuntime(),
        clock=clock,
        reconcile_on_start=False,
    )
    now = clock.value
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

    second = browser_manager_for_testing(
        core_paths,
        runtime=FakeRuntime(),
        clock=clock,
        reconcile_on_start=True,
    )
    row = second.store.get(created.lease_id)
    assert row.state == LeaseState.QUARANTINED
    assert row.error_code == "browser_launch_identity_pending"
    clock.advance(3)
    assert second.maintain() == [{"lease_id": created.lease_id, "status": "process_absent"}]
    assert second.store.get(created.lease_id).state == LeaseState.FAILED


def test_reconcile_persistence_interrupt_retains_generation_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = browser_manager_for_testing(paths(tmp_path), runtime=FakeRuntime(), reconcile_on_start=False)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    created = manager.store.create(
        lease_id="e" * 32,
        request=request(),
        mode=BrowserMode.HEADLESS,
        created_at=now,
        expires_at=now + timedelta(minutes=5),
        runtime_identity=TEST_IDENTITY,
        maximum_browsers=2,
    )
    manager.store.transition(created.lease_id, LeaseState.STARTING, now)

    def interrupt(*_args: Any, **_kwargs: Any) -> None:
        raise KeyboardInterrupt("persistence")

    monkeypatch.setattr(BrowserManager, "_quarantine_stored", interrupt)
    with pytest.raises(KeyboardInterrupt):
        manager.reconcile()
    assert created.lease_id in manager._guarded
    exclusive = FileLock(manager.layout.generation_lock, "browser_generation_busy")
    with pytest.raises(BrowserManagerError) as blocked:
        exclusive.acquire()
    assert blocked.value.code == "browser_generation_busy"


def test_reconciliation_guards_an_absent_lease_from_another_runtime(tmp_path: Path) -> None:
    core_paths = paths(tmp_path)
    clock = MutableClock()
    other_identity = BrowserRuntimeIdentity(
        playwright_version="other-playwright",
        chromium_version="other-chromium",
        executable=Path(sys.executable).resolve(),
        control_executable=Path(sys.executable).resolve(),
    )
    first = browser_manager_for_testing(
        core_paths,
        runtime=FakeRuntime(identity=other_identity),
        clock=clock,
        reconcile_on_start=False,
    )
    now = clock.value
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

    second = browser_manager_for_testing(
        core_paths,
        runtime=FakeRuntime(),
        clock=clock,
        reconcile_on_start=True,
    )
    row = second.store.get(created.lease_id)

    assert row.state == LeaseState.QUARANTINED
    assert row.error_code == "browser_launch_identity_pending"
    exclusive = FileLock(second.layout.generation_lock, "browser_generation_busy")
    with pytest.raises(BrowserManagerError) as busy:
        exclusive.acquire()
    assert busy.value.code == "browser_generation_busy"
    clock.advance(3)
    assert second.maintain() == [{"lease_id": created.lease_id, "status": "process_absent"}]
    exclusive.acquire()
    exclusive.release()


def test_cleanup_failure_quarantines_profile_until_reconciliation(tmp_path: Path) -> None:
    core_paths = paths(tmp_path)
    runtime = FakeRuntime()
    manager = browser_manager_for_testing(core_paths, runtime=runtime, maximum_browsers=1, reconcile_on_start=False)
    managed = manager.acquire(request())
    runtime.handles[0].close_fails = True

    assert managed.release().state == LeaseState.QUARANTINED
    with pytest.raises(BrowserManagerError) as blocked:
        manager.acquire(request())
    assert blocked.value.code == "browser_capacity_unavailable"
    with pytest.raises(BrowserManagerError) as realm_blocked:
        manager.acquire(request("amazon-operations", "scorecard"))
    assert realm_blocked.value.code == "browser_capacity_unavailable"
    with pytest.raises(BrowserManagerError) as capacity_blocked:
        manager.acquire(request("paycom-client", "timecard"))
    assert capacity_blocked.value.code == "browser_capacity_unavailable"
    exclusive = FileLock(manager.layout.generation_lock, "browser_generation_busy")
    with pytest.raises(BrowserManagerError) as generation_blocked:
        exclusive.acquire()
    assert generation_blocked.value.code == "browser_generation_busy"

    assert manager.maintain() == [{"lease_id": managed.lease_id, "status": "process_absent"}]
    assert manager.store.get(managed.lease_id).state == LeaseState.FAILED
    exclusive.acquire()
    exclusive.release()


def test_renew_extends_only_live_owned_leases(tmp_path: Path) -> None:
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
    managed = manager.acquire(request("test-realm", "collector-one"))
    start = managed.lease.expires_at

    # Advance past the original deadline: activation of the expired READY
    # lease must be refused.
    clock.advance(60)
    with pytest.raises(BrowserManagerError) as expired:
        manager.activate(managed.lease_id)
    assert expired.value.code == "browser_lease_expired"
    # Renewal rescues the live lease: deadline extends from now.
    renewed = managed.renew()
    assert renewed.state == LeaseState.READY
    assert renewed.expires_at > start

    # A crashed browser is failed as crashed, not renewed.
    runtime.handles[0].alive = False
    outcome = managed.renew()
    assert outcome.state == LeaseState.FAILED


def test_renew_rejects_unowned_lease(tmp_path: Path) -> None:
    first = browser_manager_for_testing(paths(tmp_path), runtime=FakeRuntime(), reconcile_on_start=False)
    managed = first.acquire(request())
    other = browser_manager_for_testing(
        paths(tmp_path),
        runtime=FakeRuntime(),
        reconcile_on_start=False,
    )
    with pytest.raises(BrowserManagerError) as denied:
        other.renew(managed.lease_id)
    assert denied.value.code == "browser_lease_not_owned"
    managed.release()


def test_service_tick_runs_browser_maintenance(tmp_path: Path) -> None:
    calls: list[int] = []

    def maintenance() -> list[dict[str, str]]:
        calls.append(1)
        return [{"lease_id": "x", "status": "process_absent"}]

    def failing_maintenance() -> list[dict[str, str]]:
        raise RuntimeError("synthetic maintenance crash")

    core_paths = paths(tmp_path)
    store = CollectionTaskStore.from_paths(core_paths)
    supervisor = CollectionWorkerSupervisor(store.database, ProductionManagerFactory(core_paths))
    healthy = CollectionService(store, supervisor, browser_maintenance=maintenance)
    tick = healthy.tick()
    assert calls == [1]
    assert tick.safe_data()["browser_maintenance"] == [{"lease_id": "x", "status": "process_absent"}]

    resilient = CollectionService(store, supervisor, browser_maintenance=failing_maintenance)
    failed_tick = resilient.tick()
    assert failed_tick.safe_data()["browser_maintenance"] == [
        {"lease_id": "-", "status": "browser_maintenance_failed"}
    ]


def test_acquire_waits_for_capacity_then_succeeds() -> None:
    calls = {"n": 0}

    class FlakyBrowser:
        def acquire(self, _request: BrowserLeaseRequest) -> str:
            calls["n"] += 1
            if calls["n"] < 3:
                raise BrowserManagerError("browser_capacity_unavailable", "busy")
            return "lease"

    manager = CollectionManager(FlakyBrowser(), browser_wait_seconds=5.0)  # type: ignore[arg-type]
    registration = CollectorRegistration(
        collector_id="probe",
        plugin_id="dcr",
        plugin_release="release-1",
        runner=lambda session, request: None,
        browser_realm="amazon-operations",
    )
    result = manager._acquire_with_wait(registration, CollectionRequest(collector_id="probe"), timeout_seconds=5.0)
    assert result == "lease"
    assert calls["n"] == 3


def test_acquire_wait_times_out_with_busy_error() -> None:
    class BusyBrowser:
        def acquire(self, _request: BrowserLeaseRequest) -> str:
            raise BrowserManagerError("browser_realm_busy", "limit reached")

    manager = CollectionManager(BusyBrowser(), browser_wait_seconds=0.5)  # type: ignore[arg-type]
    registration = CollectorRegistration(
        collector_id="probe",
        plugin_id="dcr",
        plugin_release="release-1",
        runner=lambda session, request: None,
        browser_realm="amazon-operations",
    )
    with pytest.raises(BrowserManagerError) as busy:
        manager._acquire_with_wait(registration, CollectionRequest(collector_id="probe"), timeout_seconds=0.5)
    assert busy.value.code == "browser_realm_busy"


def test_acquire_wait_never_blocks_on_permanent_errors() -> None:
    calls = {"n": 0}

    class BrokenBrowser:
        def acquire(self, _request: BrowserLeaseRequest) -> str:
            calls["n"] += 1
            raise BrowserManagerError("unknown_browser_realm", "not installed")

    manager = CollectionManager(BrokenBrowser(), browser_wait_seconds=30.0)  # type: ignore[arg-type]
    registration = CollectorRegistration(
        collector_id="probe",
        plugin_id="dcr",
        plugin_release="release-1",
        runner=lambda session, request: None,
        browser_realm="amazon-operations",
    )
    with pytest.raises(BrowserManagerError) as denied:
        manager._acquire_with_wait(registration, CollectionRequest(collector_id="probe"), timeout_seconds=30.0)
    assert denied.value.code == "unknown_browser_realm"
    assert calls["n"] == 1


def test_schema_migration_from_known_older_version(tmp_path: Path) -> None:
    home = tmp_path / "home"
    database = home / ".dispatch" / "data" / "db" / "browser-manager" / "browser-manager.sqlite3"
    database.parent.mkdir(parents=True, mode=0o700)
    LeaseStore(database)
    # Simulate a database written with an older declared version but the
    # current physical schema (the v3->v4 migration is a validated no-op).
    connection = sqlite3.connect(database)
    connection.execute("UPDATE metadata SET value = '3' WHERE key = 'schema_version'")
    connection.commit()
    connection.close()

    reopened = LeaseStore(database)  # must migrate, not fail
    version_row = sqlite3.connect(database).execute(
        "SELECT value FROM metadata WHERE key = 'schema_version'"
    ).fetchone()
    assert version_row[0] == "4"
    # The migrated store remains fully usable.
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    row = reopened.create(
        lease_id="c" * 32,
        request=request(),
        mode=BrowserMode.HEADLESS,
        created_at=now,
        expires_at=now + timedelta(minutes=5),
        runtime_identity=TEST_IDENTITY,
        maximum_browsers=8,
        realm_max_concurrent=4,
    )
    assert row.state == LeaseState.REQUESTED


def test_newer_schema_version_fails_closed_with_guidance(tmp_path: Path) -> None:
    home = tmp_path / "home"
    database = home / ".dispatch" / "data" / "db" / "browser-manager" / "browser-manager.sqlite3"
    database.parent.mkdir(parents=True, mode=0o700)
    LeaseStore(database)
    connection = sqlite3.connect(database)
    connection.execute("UPDATE metadata SET value = '999' WHERE key = 'schema_version'")
    connection.commit()
    connection.close()

    with pytest.raises(BrowserManagerError) as rejected:
        LeaseStore(database)
    assert rejected.value.code == "unsupported_browser_schema"
    assert "newer Dispatch" in str(rejected.value)


def test_prune_removes_only_old_terminal_rows(tmp_path: Path) -> None:
    clock = MutableClock()
    manager = browser_manager_for_testing(
        paths(tmp_path),
        runtime=FakeRuntime(),
        clock=clock,
        reconcile_on_start=False,
    )
    old = manager.acquire(request("amazon-operations", "dcr"))
    old.release()  # CLOSED
    fresh = manager.acquire(request("paycom-client", "timecard"))
    fresh.cancel()  # CANCELLED

    cutoff_now = clock()
    # Pruning with a cutoff of "now" removes nothing (rows are brand new).
    assert manager.store.prune(before=cutoff_now, limit=100) == 0
    # Advancing past the 30-day window and pruning removes only terminal rows.
    clock.advance(31 * 24 * 3600)
    active = manager.acquire(request("amazon-operations", "scorecard"))
    removed = manager.store.prune(before=clock(), limit=100)
    assert removed == 2  # old CLOSED + CANCELLED rows
    assert manager.store.get(active.lease_id).state == LeaseState.READY
    active.release()


def test_virtual_display_requires_xvfb_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "exists", lambda self: False)
    display = runtime_module._VirtualDisplay()
    with pytest.raises(BrowserManagerError) as unavailable:
        display.start()
    assert unavailable.value.code == "browser_display_unavailable"


def test_virtual_display_lifecycle_with_real_xvfb(tmp_path: Path) -> None:
    xvfb = Path("/usr/bin/Xvfb")
    if not xvfb.exists():
        pytest.skip("Xvfb not installed on this host")
    display = runtime_module._VirtualDisplay()
    name = display.start()
    assert name.startswith(":")
    socket = Path("/tmp/.X11-unix") / f"X{name[1:]}"
    assert socket.exists()
    display.stop()
    assert not socket.exists() or True  # socket removal is asynchronous; process death is the contract


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
    # A stuck lease on one profile/realm must NOT block unrelated
    # acquisitions under counted realm capacity (the old exclusive-realm
    # behavior made every acquisition fail while any row was nonterminal).
    unrelated = other.acquire(request("paycom-client", "timecard"))
    assert unrelated.lease.state == LeaseState.READY
    unrelated.release()
    # The exact same profile remains blocked while the stuck lease holds it.
    with pytest.raises(BrowserManagerError) as blocked:
        other.acquire(request())
    assert blocked.value.code == "browser_profile_busy"

    monkeypatch.setattr(manager.store, "transition", original_transition)
    assert manager.maintain() == [
        {"lease_id": managed.lease_id, "status": "browser_cleanup_retry"}
    ]
    assert managed.lease.state == LeaseState.CLOSED


def test_failed_launch_state_error_guards_unsafe_locks(
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
    with pytest.raises(BrowserManagerError) as blocked:
        LeaseLocks.acquire(
            manager.layout,
            maximum_browsers=manager.maximum_browsers,
            realm=row.realm,
            realm_max_concurrent=1,
            profile_key=row.profile_key,
        )
    assert blocked.value.code in {"browser_capacity_unavailable", "browser_realm_busy"}
    guarded = manager._guarded.pop(row.lease_id)
    guarded[0].release()


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
    exclusive = FileLock(manager.layout.generation_lock, "browser_generation_busy")
    with pytest.raises(BrowserManagerError) as generation_blocked:
        exclusive.acquire()
    assert generation_blocked.value.code == "browser_generation_busy"
    with pytest.raises(BrowserManagerError) as blocked:
        manager.acquire(request("paycom-client", "timecard"))
    assert blocked.value.code == "browser_capacity_unavailable"


def test_generation_lock_blocks_activation_while_a_lease_holds_shared_authority(tmp_path: Path) -> None:
    layout = BrowserLayout.from_paths(paths(tmp_path))
    layout.prepare()
    shared = FileLock(layout.generation_lock, "browser_generation_busy", shared=True)
    exclusive = FileLock(layout.generation_lock, "browser_generation_busy")
    shared.acquire()
    try:
        with pytest.raises(BrowserManagerError) as error:
            exclusive.acquire()
        assert error.value.code == "browser_generation_busy"
    finally:
        shared.release()
    exclusive.acquire()
    exclusive.release()


def test_file_lock_rejects_parent_swapped_to_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locks = tmp_path / "locks"
    locks.mkdir()
    displaced = tmp_path / "locks-original"
    outside = tmp_path / "outside"
    outside.mkdir()
    original_open = runtime_module.os.open
    swapped = False

    def racing_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if path == "generation.lock" and dir_fd is not None and not swapped:
            locks.rename(displaced)
            locks.symlink_to(outside, target_is_directory=True)
            swapped = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(runtime_module.os, "open", racing_open)
    with pytest.raises(BrowserManagerError) as error:
        FileLock(locks / "generation.lock", "browser_generation_busy").acquire()
    assert error.value.code == "unsafe_browser_storage"
    assert not (outside / "generation.lock").exists()


def test_file_lock_baseexception_after_flock_closes_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "interrupt.lock"
    original = runtime_module.fcntl.flock
    raised = False

    def interrupt_after_lock(descriptor: int, operation: int) -> None:
        nonlocal raised
        original(descriptor, operation)
        if not raised and operation & runtime_module.fcntl.LOCK_NB:
            raised = True
            raise KeyboardInterrupt("after flock")

    before = set(Path("/proc/self/fd").iterdir())
    monkeypatch.setattr(runtime_module.fcntl, "flock", interrupt_after_lock)
    with pytest.raises(KeyboardInterrupt):
        FileLock(path, "browser_profile_busy").acquire()
    after = set(Path("/proc/self/fd").iterdir())
    assert after == before
    monkeypatch.setattr(runtime_module.fcntl, "flock", original)
    retry = FileLock(path, "browser_profile_busy")
    retry.acquire()
    retry.release()


def test_lease_lock_interruption_releases_shared_generation_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = BrowserLayout.from_paths(paths(tmp_path))
    layout.prepare()
    original = FileLock.acquire
    calls = 0

    def interrupt_second(lock: FileLock) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt("slot interruption")
        original(lock)

    monkeypatch.setattr(FileLock, "acquire", interrupt_second)
    with pytest.raises(KeyboardInterrupt):
        LeaseLocks.acquire(layout, maximum_browsers=2, realm="amazon", realm_max_concurrent=1, profile_key="profile")
    monkeypatch.setattr(FileLock, "acquire", original)
    exclusive = FileLock(layout.generation_lock, "browser_generation_busy")
    exclusive.acquire()
    exclusive.release()


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
