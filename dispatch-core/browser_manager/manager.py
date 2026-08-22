"""Browser lease coordination, supervision, and crash reconciliation."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import os
from pathlib import Path
import secrets
import threading


from paths import DispatchPaths

from .models import (
    BrowserLease,
    BrowserLeaseRequest,
    BrowserManagerError,
    LeaseState,
    ManagedBrowserSession,
    TERMINAL_STATES,
    utc_now,
)
from .policy import RealmRegistry
from .runtime import (
    BrowserLayout,
    BrowserRuntime,
    LeaseLocks,
    PlaywrightRuntime,
    RuntimeHandle,
    matching_browser_processes,
    process_start_ticks,
    terminate_control_process,
    terminate_owned_process,
)
from .store import LeaseRow, LeaseStore


@dataclass
class _ActiveLease:
    handle: RuntimeHandle
    locks: LeaseLocks
    profile: Path
    # Serializes teardown of this specific lease. _close_active claims the
    # entry under __leases_lock, then releases __leases_lock before doing the
    # slow work (handle.close(), store transitions); without a per-lease lock,
    # two closers could run handle.close() concurrently on non-thread-safe
    # Playwright objects and double-terminate process trees.
    close_lock: threading.Lock = field(default_factory=threading.Lock)
    requested_final_state: LeaseState | None = None
    requested_error_code: str | None = None


_DEFAULT_MAXIMUM_BROWSERS = 8


def _configured_maximum_browsers() -> int:
    """Deployment capacity override; defaults to 8 concurrent browsers."""

    raw = os.environ.get("DISPATCH_BROWSER_CAPACITY", "").strip()
    if not raw:
        return _DEFAULT_MAXIMUM_BROWSERS
    try:
        value = int(raw)
    except ValueError as exc:
        raise BrowserManagerError(
            "invalid_browser_policy",
            "DISPATCH_BROWSER_CAPACITY must be an integer",
        ) from exc
    if not 1 <= value <= 64:
        raise BrowserManagerError(
            "invalid_browser_policy",
            "DISPATCH_BROWSER_CAPACITY must be between 1 and 64",
        )
    return value


class ManagedLease:
    """Internal lease capability; never serialize this object into a Core response."""

    def __init__(self, manager: "BrowserManager", lease_id: str) -> None:
        self._manager = manager
        self.lease_id = lease_id

    @property
    def lease(self) -> BrowserLease:
        return self._manager.lease(self.lease_id)

    @property
    def session(self) -> ManagedBrowserSession:
        return self._manager.session(self.lease_id)

    def activate(self) -> BrowserLease:
        return self._manager.activate(self.lease_id)

    def renew(self) -> BrowserLease:
        return self._manager.renew(self.lease_id)

    def release(self) -> BrowserLease:
        return self._manager.release(self.lease_id)

    def cancel(self) -> BrowserLease:
        return self._manager.cancel(self.lease_id)

    def __enter__(self) -> "ManagedLease":
        self.activate()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if exc_type is None:
            self.release()
        else:
            self._manager.fail(self.lease_id, "browser_consumer_failed")


class BrowserManager:
    """Core-owned browser lifecycle manager with durable leases and private profiles."""

    __slots__ = (
        "__runtime",
        "__realms",
        "__maximum_browsers",
        "__clock",
        "__layout",
        "__store",
        "__leases_lock",
        "_active",
        "_guarded",
    )

    def __init__(self, paths: DispatchPaths, *, reconciliation_only: bool = False) -> None:
        self.__runtime: BrowserRuntime | None = None if reconciliation_only else PlaywrightRuntime()
        self.__realms = RealmRegistry()
        self.__maximum_browsers = _configured_maximum_browsers()
        self.__clock = utc_now
        self.__layout = BrowserLayout.from_paths(paths)
        self.__layout.prepare()
        self.__store = LeaseStore(self.__layout.database)
        # Guards the in-process lease maps; the store has its own SQLite-level
        # serialization, but _active/_guarded are plain dicts that acquire(),
        # _close_active(), maintain(), and reconcile() mutate concurrently.
        self.__leases_lock = threading.RLock()
        self._active: dict[str, _ActiveLease] = {}
        self._guarded: dict[str, tuple[LeaseLocks, Path]] = {}
        if not reconciliation_only:
            self.reconcile()

    @property
    def layout(self) -> BrowserLayout:
        return self.__layout

    @property
    def store(self) -> LeaseStore:
        return self.__store

    @property
    def maximum_browsers(self) -> int:
        return self.__maximum_browsers

    def realm_concurrency(self, realm_id: str) -> int:
        """Realm concurrency limit for lock acquisition; 1 when uninstalled/unknown."""

        try:
            return self.__realms.get(realm_id).max_concurrent_leases
        except BrowserManagerError:
            return 1

    def acquire(self, request: BrowserLeaseRequest) -> ManagedLease:
        runtime = self.__runtime
        if runtime is None:
            raise BrowserManagerError("playwright_missing", "browser launch is unavailable in reconciliation-only mode")
        realm = self.__realms.resolve(request)
        mode = request.mode or realm.default_mode
        profile = self.layout.profile(request.realm, request.plugin_id, request.account_alias)
        locks = LeaseLocks.acquire(
            self.layout,
            profile_key=request.profile_key,
            realm=realm.id,
            realm_max_concurrent=realm.max_concurrent_leases,
            maximum_browsers=self.maximum_browsers,
        )
        lease_id = secrets.token_hex(16)
        created_at = self.__clock()
        expires_at = created_at + timedelta(seconds=realm.lease_timeout_seconds)
        runtime_identity = runtime.identity
        row: LeaseRow | None = None
        handle: RuntimeHandle | None = None
        try:
            row = self.store.create(
                lease_id=lease_id,
                request=request,
                mode=mode,
                created_at=created_at,
                expires_at=expires_at,
                runtime_identity=runtime_identity,
                maximum_browsers=self.maximum_browsers,
                realm_max_concurrent=realm.max_concurrent_leases,
            )
            row = self.store.transition(lease_id, LeaseState.STARTING, self.__clock())
            handle = runtime.start(
                lease_id=lease_id,
                profile=profile,
                realm=realm,
                mode=mode,
                record_control_process=lambda pid, ticks: self.store.attach_control_process(
                    lease_id,
                    pid=pid,
                    process_start_ticks=ticks,
                    at=self.__clock(),
                ),
            )
            if handle.identity != runtime_identity:
                raise BrowserManagerError(
                    "browser_runtime_changed",
                    "browser runtime identity changed during launch",
                )
            self.store.attach_process(
                lease_id,
                pid=handle.pid,
                process_start_ticks=handle.process_start_ticks,
                at=self.__clock(),
            )
            row = self.store.transition(lease_id, LeaseState.READY, self.__clock())
            with self.__leases_lock:
                self._active[lease_id] = _ActiveLease(handle=handle, locks=locks, profile=profile)
            return ManagedLease(self, lease_id)
        except BaseException as exc:
            cleanup_failed = self._error_code(exc) == "browser_cleanup_failed"
            if handle is not None:
                try:
                    handle.close()
                except BaseException:
                    cleanup_failed = True
            state_error: BaseException | None = None
            if row is not None:
                try:
                    if cleanup_failed:
                        self._quarantine_stored(row.lease_id, "browser_cleanup_failed")
                    else:
                        self._fail_stored(row.lease_id, self._error_code(exc))
                except BaseException as persistence_exc:
                    state_error = persistence_exc
            if (cleanup_failed or state_error is not None) and row is not None:
                with self.__leases_lock:
                    self._guarded[row.lease_id] = (locks, profile)
            else:
                locks.release()
            if state_error is not None:
                raise BrowserManagerError(
                    "browser_state_unavailable",
                    "browser startup ownership could not be persisted safely",
                ) from state_error
            if cleanup_failed:
                raise BrowserManagerError(
                    "browser_cleanup_failed",
                    "browser startup failed and the owned process could not be cleaned up",
                ) from exc
            raise

    def lease(self, lease_id: str) -> BrowserLease:
        return self.store.get(lease_id).lease()

    def session(self, lease_id: str) -> ManagedBrowserSession:
        with self.__leases_lock:
            active = self._active.get(lease_id)
        if active is None:
            raise BrowserManagerError("browser_lease_not_owned", "browser lease is not active in this manager")
        return active.handle.session

    def renew(self, lease_id: str) -> BrowserLease:
        """Extend an owned, live lease by its realm's lease timeout.

        Renewal requires positive proof the browser process still matches
        its recorded identity; a dead browser is failed as crashed instead of
        being silently renewed.
        """

        with self.__leases_lock:
            active = self._active.get(lease_id)
        if active is None:
            raise BrowserManagerError("browser_lease_not_owned", "browser lease is not active in this manager")
        row = self.store.get(lease_id)
        if row.state in TERMINAL_STATES:
            return row.lease()
        if row.state == LeaseState.CLOSING or not active.handle.is_alive():
            return self._close_active(lease_id, LeaseState.FAILED, "browser_crashed")
        realm = self.__realms.get(row.realm)
        return self.store.renew(
            lease_id,
            at=self.__clock(),
            lease_seconds=realm.lease_timeout_seconds,
        ).lease()

    def activate(self, lease_id: str) -> BrowserLease:
        with self.__leases_lock:
            owned = lease_id in self._active
        if not owned:
            raise BrowserManagerError("browser_lease_not_owned", "browser lease is not active in this manager")
        row = self.store.get(lease_id)
        if row.state == LeaseState.ACTIVE:
            return row.lease()
        if row.expires_at <= self.__clock():
            # An expired READY lease must never grant a full session; let
            # maintain() reconcile it instead.
            raise BrowserManagerError("browser_lease_expired", "browser lease expired before activation")
        return self.store.transition(lease_id, LeaseState.ACTIVE, self.__clock()).lease()

    def release(self, lease_id: str) -> BrowserLease:
        return self._close_active(lease_id, LeaseState.CLOSED, None)

    def cancel(self, lease_id: str) -> BrowserLease:
        return self._close_active(lease_id, LeaseState.CANCELLED, "browser_lease_cancelled")

    def fail(self, lease_id: str, error_code: str) -> BrowserLease:
        return self._close_active(lease_id, LeaseState.FAILED, error_code)

    def _close_active(
        self,
        lease_id: str,
        final_state: LeaseState,
        error_code: str | None,
    ) -> BrowserLease:
        # Claim the entry atomically: pop from _active and stamp the first
        # requested outcome in ONE critical section so a concurrent closer
        # (consumer thread vs maintain()) can never double-start teardown or
        # apply a second, different final state. First request wins.
        with self.__leases_lock:
            active = self._active.pop(lease_id, None)
            if active is not None and active.requested_final_state is None:
                active.requested_final_state = final_state
                active.requested_error_code = error_code
            elif active is not None and active.requested_final_state is not None:
                final_state = active.requested_final_state
                error_code = active.requested_error_code
        if active is None:
            row = self.store.get(lease_id)
            if row.state in TERMINAL_STATES:
                return row.lease()
            raise BrowserManagerError("browser_lease_not_owned", "browser lease is not active in this manager")
        try:
            # Serialize the slow teardown against any closer that claimed the
            # entry before us (they popped it; we only hold a stale reference).
            with active.close_lock:
                if active.requested_final_state is not None and final_state != active.requested_final_state:
                    final_state = active.requested_final_state
                    error_code = active.requested_error_code
                row = self.store.get(lease_id)
                if row.state != LeaseState.CLOSING:
                    row = self.store.transition(lease_id, LeaseState.CLOSING, self.__clock())
                try:
                    active.handle.close()
                except (KeyboardInterrupt, SystemExit) as exc:
                    if active.handle.is_alive():
                        self.store.transition(
                            lease_id,
                            LeaseState.QUARANTINED,
                            self.__clock(),
                            error_code=self._error_code(exc, "browser_cleanup_interrupted"),
                        )
                        with self.__leases_lock:
                            self._guarded[lease_id] = (active.locks, active.profile)
                    else:
                        self.store.transition(
                            lease_id,
                            final_state,
                            self.__clock(),
                            error_code=error_code,
                        )
                        active.locks.release()
                    raise
                except BaseException as exc:
                    quarantined = self.store.transition(
                        lease_id,
                        LeaseState.QUARANTINED,
                        self.__clock(),
                        error_code=self._error_code(exc, "browser_cleanup_failed"),
                    ).lease()
                    with self.__leases_lock:
                        self._guarded[lease_id] = (active.locks, active.profile)
                    return quarantined
                completed = self.store.transition(
                    lease_id,
                    final_state,
                    self.__clock(),
                    error_code=error_code,
                ).lease()
                active.locks.release()
                return completed
        except BaseException:
            # Teardown began but did not reach a durable terminal/quarantine
            # state (e.g. the store raised before handle.close()). Re-arm the
            # entry so maintain() retries the requested outcome instead of
            # losing ownership of the lease.
            row = self.store.get(lease_id)
            if (
                row.state in {LeaseState.ACTIVE, LeaseState.READY, LeaseState.STARTING}
                and lease_id not in self._guarded
            ):
                with self.__leases_lock:
                    self._active[lease_id] = active
            raise

    def maintain(self) -> list[dict[str, str]]:
        """Run in the Core service loop to enforce crashes and lease deadlines."""

        now = self.__clock()
        outcomes: list[dict[str, str]] = []
        with self.__leases_lock:
            active_snapshot = list(self._active.items())
        for lease_id, active in active_snapshot:
            # One failing lease (store hiccup, unexpected /proc error) must
            # never starve crash/expiry enforcement of the remaining leases.
            try:
                row = self.store.get(lease_id)
                if active.requested_final_state is not None:
                    self._close_active(
                        lease_id,
                        active.requested_final_state,
                        active.requested_error_code,
                    )
                    outcomes.append({"lease_id": lease_id, "status": "browser_cleanup_retry"})
                elif row.state == LeaseState.CLOSING:
                    self._close_active(lease_id, LeaseState.FAILED, "browser_cleanup_retry")
                    outcomes.append({"lease_id": lease_id, "status": "browser_cleanup_retry"})
                elif not active.handle.is_alive():
                    self._close_active(lease_id, LeaseState.FAILED, "browser_crashed")
                    outcomes.append({"lease_id": lease_id, "status": "browser_crashed"})
                elif row.expires_at <= now:
                    self._close_active(lease_id, LeaseState.FAILED, "browser_lease_expired")
                    outcomes.append({"lease_id": lease_id, "status": "browser_lease_expired"})
            except Exception as exc:
                outcomes.append(
                    {"lease_id": lease_id, "status": self._error_code(exc, "browser_maintenance_failed")}
                )
        for row in self.store.nonterminal():
            with self.__leases_lock:
                if (
                    row.state != LeaseState.QUARANTINED
                    and row.lease_id not in self._guarded
                ) or row.lease_id in self._active:
                    continue
            try:
                outcome = self._reconcile_row(row)
            except Exception as exc:
                outcome = {"lease_id": row.lease_id, "status": self._error_code(exc, "browser_maintenance_failed")}
            if outcome is not None:
                outcomes.append(outcome)
        # Bounded retention: prune terminal rows older than 30 days so the
        # ledger cannot grow without bound. Failures never break maintenance.
        try:
            pruned = self.store.prune(before=now - timedelta(days=30), limit=500)
            if pruned:
                outcomes.append({"lease_id": "-", "status": f"pruned_{pruned}"})
        except Exception as exc:
            outcomes.append({"lease_id": "-", "status": self._error_code(exc, "browser_maintenance_failed")})
        return outcomes

    def reconcile(self) -> list[dict[str, str]]:
        """Fail interrupted leases and stop only positively identified owned browsers."""

        outcomes: list[dict[str, str]] = []
        for row in self.store.nonterminal():
            with self.__leases_lock:
                owned = row.lease_id in self._active
            if owned:
                outcomes.append({"lease_id": row.lease_id, "status": "owned_locally"})
                continue
            try:
                outcome = self._reconcile_row(row)
            except Exception as exc:
                outcome = {"lease_id": row.lease_id, "status": self._error_code(exc, "browser_maintenance_failed")}
            if outcome is not None:
                outcomes.append(outcome)
        return outcomes

    def _reconcile_row(self, row: LeaseRow) -> dict[str, str] | None:
        try:
            request = BrowserLeaseRequest(
                plugin_id=row.plugin_id,
                plugin_release=row.plugin_release,
                realm=row.realm,
                purpose=row.purpose,
                account_alias=row.account_alias,
                mode=row.mode,
            )
            profile = self.layout.profile(request.realm, request.plugin_id, request.account_alias)
        except Exception as exc:
            # A stored row that no longer validates must not abort
            # reconciliation of the remaining leases.
            self._quarantine_stored(row.lease_id, self._error_code(exc, "browser_lease_record_invalid"))
            return {"lease_id": row.lease_id, "status": "browser_lease_record_invalid"}
        with self.__leases_lock:
            guarded = self._guarded.get(row.lease_id)
        reconcile_locks: LeaseLocks | None = None
        if guarded is None:
            try:
                reconcile_locks = LeaseLocks.acquire(
                    self.layout,
                    profile_key=request.profile_key,
                    realm=request.realm,
                    realm_max_concurrent=self.realm_concurrency(request.realm),
                    maximum_browsers=self.maximum_browsers,
                )
            except BrowserManagerError as exc:
                if exc.code in {
                    "browser_generation_busy",
                    "browser_capacity_unavailable",
                    "browser_realm_busy",
                    "browser_profile_busy",
                }:
                    return {"lease_id": row.lease_id, "status": "owned_elsewhere"}
                raise
        status = "interrupted"
        persisted = False
        unsafe = True
        try:
            browser_pair = row.pid is not None and row.process_start_ticks is not None
            control_pair = row.control_pid is not None and row.control_process_start_ticks is not None
            partial_identity = (row.pid is None) != (row.process_start_ticks is None) or (
                (row.control_pid is None) != (row.control_process_start_ticks is None)
            )
            if partial_identity:
                status = "browser_process_identity_missing"
            else:
                if browser_pair:
                    assert row.pid is not None and row.process_start_ticks is not None
                    try:
                        terminated = terminate_owned_process(
                            pid=row.pid,
                            expected_start_ticks=row.process_start_ticks,
                            profile=profile,
                            expected_executable=row.runtime_executable,
                        )
                        status = "orphan_terminated" if terminated else "process_absent"
                    except BrowserManagerError as exc:
                        status = exc.code
                if control_pair and status not in {
                    "browser_process_identity_mismatch",
                    "browser_cleanup_failed",
                }:
                    assert row.control_pid is not None and row.control_process_start_ticks is not None
                    try:
                        terminated = terminate_control_process(
                            row.control_pid,
                            row.control_process_start_ticks,
                            row.runtime_control_executable,
                        )
                        if terminated:
                            status = "control_tree_terminated"
                    except BrowserManagerError as exc:
                        status = exc.code

                if not browser_pair:
                    matches = matching_browser_processes(profile)
                    if len(matches) == 1:
                        ticks = process_start_ticks(matches[0])
                        if ticks is None:
                            status = "browser_process_identity_missing"
                        else:
                            try:
                                terminated = terminate_owned_process(
                                    matches[0],
                                    ticks,
                                    profile,
                                    row.runtime_executable,
                                )
                                status = "orphan_terminated" if terminated else "process_absent"
                            except BrowserManagerError as exc:
                                status = exc.code
                    elif len(matches) > 1:
                        status = "browser_process_identity_ambiguous"
                    elif not control_pair:
                        grace_elapsed = (
                            row.state == LeaseState.QUARANTINED
                            and row.error_code == "browser_launch_identity_pending"
                            and (self.__clock() - row.updated_at).total_seconds() >= 2
                        )
                        status = "process_absent" if grace_elapsed else "browser_launch_identity_pending"
            unsafe = status in {
                "browser_cleanup_failed",
                "browser_control_identity_mismatch",
                "browser_launch_identity_pending",
                "browser_process_identity_ambiguous",
                "browser_process_identity_mismatch",
                "browser_process_identity_missing",
                "browser_runtime_identity_mismatch",
            }
            if unsafe:
                self._quarantine_stored(row.lease_id, status)
            else:
                self._fail_stored(row.lease_id, status)
            persisted = True
            return {"lease_id": row.lease_id, "status": status}
        finally:
            keep_guarded = not persisted or unsafe
            with self.__leases_lock:
                if reconcile_locks is not None:
                    if keep_guarded:
                        self._guarded[row.lease_id] = (reconcile_locks, profile)
                if guarded is not None and not keep_guarded:
                    self._guarded.pop(row.lease_id, None)
            if reconcile_locks is not None and not keep_guarded:
                reconcile_locks.release()
            if guarded is not None and not keep_guarded:
                guarded[0].release()

    def shutdown(self) -> list[BrowserLease]:
        closed: list[BrowserLease] = []
        with self.__leases_lock:
            active_ids = list(self._active)
        for lease_id in active_ids:
            closed.append(self._close_active(lease_id, LeaseState.CANCELLED, "browser_manager_shutdown"))
        with self.__leases_lock:
            has_guarded = bool(self._guarded)
        if has_guarded:
            self.reconcile()
        return closed

    def status(self, limit: int = 50) -> list[dict[str, object]]:
        return [row.safe_data() for row in self.store.recent(limit)]

    def _fail_stored(self, lease_id: str, error_code: str) -> None:
        row = self.store.get(lease_id)
        if row.state in TERMINAL_STATES:
            return
        self.store.transition(
            lease_id,
            LeaseState.FAILED,
            self.__clock(),
            error_code=error_code[:64],
        )

    def _quarantine_stored(self, lease_id: str, error_code: str) -> None:
        row = self.store.get(lease_id)
        if row.state == LeaseState.QUARANTINED or row.state in TERMINAL_STATES:
            return
        self.store.transition(
            lease_id,
            LeaseState.QUARANTINED,
            self.__clock(),
            error_code=error_code[:64],
        )

    @staticmethod
    def _error_code(exc: BaseException, fallback: str = "browser_manager_failed") -> str:
        if isinstance(exc, BrowserManagerError):
            return exc.code[:64]
        return fallback

__all__ = ["BrowserManager", "ManagedLease"]
