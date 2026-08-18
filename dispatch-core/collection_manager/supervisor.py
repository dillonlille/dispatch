"""Bounded process supervision for synchronous collection workers."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
import multiprocessing
from multiprocessing.connection import Connection
import os
from pathlib import Path
import secrets
import signal
import time
from typing import Callable, Protocol

from browser_manager import BrowserManager
from paths import DispatchPaths

from . import CollectionManager, CollectorRegistration
from .queue import CollectionStoreError, CollectionTaskStore, TaskRecord, TaskState, utc_now


_SUPERVISOR_SQLITE_TIMEOUT_SECONDS = 0.25


class ManagerFactory(Protocol):
    def __call__(self) -> CollectionManager: ...


@dataclass(frozen=True)
class ProductionManagerFactory:
    """Create all process-owned Core dependencies after the worker starts."""

    paths: DispatchPaths
    registrations: tuple[CollectorRegistration, ...] = ()

    def __call__(self) -> CollectionManager:
        needs_browser = any(item.browser_realm is not None for item in self.registrations)
        needs_authentication = any(item.authentication_required for item in self.registrations)
        browser = BrowserManager(self.paths) if needs_browser else None
        if needs_authentication:
            from authentication import AuthenticationManager

            authentication = AuthenticationManager(self.paths)
        else:
            authentication = None
        manager = CollectionManager.production(
            self.paths,
            browser_manager=browser,
            authentication=authentication,
        )
        for registration in self.registrations:
            manager.register(registration)
        return manager


@dataclass(frozen=True)
class WorkerPolicy:
    lease_seconds: int = 60
    heartbeat_seconds: float = 10.0
    startup_timeout_seconds: float = 15.0
    execution_timeout_seconds: float = 900.0
    manual_timeout_seconds: float = 900.0
    termination_grace_seconds: float = 2.0

    def __post_init__(self) -> None:
        if not 30 <= self.lease_seconds <= 3600:
            raise ValueError("worker lease must be between 30 and 3600 seconds")
        if not 0.05 <= self.heartbeat_seconds <= self.lease_seconds / 2:
            raise ValueError("worker heartbeat interval is invalid")
        if not 0.1 <= self.startup_timeout_seconds <= 60:
            raise ValueError("worker startup timeout is invalid")
        if not 0.1 <= self.execution_timeout_seconds <= 86_400:
            raise ValueError("worker execution timeout is invalid")
        if not 1 <= self.manual_timeout_seconds <= 86_400:
            raise ValueError("worker manual timeout is invalid")
        if not 0.05 <= self.termination_grace_seconds <= 10:
            raise ValueError("worker termination grace is invalid")


@dataclass(frozen=True)
class WorkerOutcome:
    status: str
    worker_id: str
    task_id: str | None
    task_state: str | None
    process_cleaned: bool

    @property
    def ok(self) -> bool:
        return self.status in {"idle", "succeeded", "waiting_for_user"}

    def safe_data(self) -> dict[str, object]:
        return {
            "status": self.status,
            "worker_id": self.worker_id,
            "task_id": self.task_id,
            "task_state": self.task_state,
            "process_cleaned": self.process_cleaned,
        }


def _event(connection: Connection, kind: str, **values: object) -> None:
    connection.send({"kind": kind, **values})


def _worker_main(
    factory: ManagerFactory,
    worker_id: str,
    lease_seconds: int,
    connection: Connection,
) -> None:
    try:
        os.setsid()
        _event(connection, "ready")
        if connection.recv() != {"command": "start"}:
            return
        manager = factory()
        claimed = manager.claim_next(worker_id, lease_seconds)
        if claimed is None:
            _event(connection, "idle")
            return
        _event(connection, "claimed", task_id=claimed.task_id)
        if connection.recv() != {"command": "execute"}:
            return
        task = manager.run_claimed(claimed.task_id, worker_id)
        while task.state == TaskState.WAITING_FOR_USER:
            _event(connection, "waiting", task_id=task.task_id)
            command = connection.recv()
            if command == {"command": "resume"}:
                task = manager.resume_task(task.task_id, lease_seconds)
            elif command == {"command": "cancel"}:
                task = manager.cancel_task(task.task_id)
            else:
                return
        _event(connection, "result", task_id=task.task_id, state=task.state.value)
    except (EOFError, BrokenPipeError):
        return
    except BaseException:
        try:
            _event(connection, "error", code="collection_worker_failed")
        except (EOFError, BrokenPipeError, OSError):
            pass
    finally:
        connection.close()


def _process_start_ticks(pid: int) -> int | None:
    try:
        value = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        close = value.rfind(")")
        if close < 0:
            return None
        return int(value[close + 2 :].split()[19])
    except (FileNotFoundError, OSError, ValueError, IndexError):
        return None


def _process_state(pid: int) -> str | None:
    try:
        value = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        close = value.rfind(")")
        if close < 0:
            return None
        return value[close + 2 :].split()[0]
    except (FileNotFoundError, OSError, IndexError):
        return None


def _group_exists(group_id: int) -> bool:
    try:
        os.killpg(group_id, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _group_has_live_members(group_id: int) -> bool:
    try:
        entries = tuple(Path("/proc").iterdir())
    except OSError:
        return _group_exists(group_id)
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            value = (entry / "stat").read_text(encoding="utf-8")
            close = value.rfind(")")
            fields = value[close + 2 :].split()
            if close >= 0 and int(fields[2]) == group_id and fields[0] != "Z":
                return True
        except (FileNotFoundError, OSError, ValueError, IndexError):
            continue
    return False


def _stop_group(pid: int, start_ticks: int, grace_seconds: float) -> bool:
    observed = _process_start_ticks(pid)
    if observed is None:
        return not _group_exists(pid)
    if observed != start_ticks:
        return False
    try:
        if os.getpgid(pid) != pid:
            return False
    except ProcessLookupError:
        return not _group_exists(pid)
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
    observed = _process_start_ticks(pid)
    if observed == start_ticks:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    final_deadline = time.monotonic() + grace_seconds
    while time.monotonic() < final_deadline and _group_has_live_members(pid):
        time.sleep(0.01)
    return not _group_has_live_members(pid)


class CollectionWorkerSupervisor:
    """Start one isolated worker, heartbeat it, enforce deadlines, and clean its process group."""

    def __init__(
        self,
        database: Path,
        factory: ManagerFactory,
        policy: WorkerPolicy = WorkerPolicy(),
        *,
        context: str = "spawn",
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if context != "spawn":
            raise ValueError("collection workers require the spawn process context")
        self._database = database
        self._factory = factory
        self._policy = policy
        self._context = multiprocessing.get_context(context)
        self._clock = clock

    def run_once(
        self,
        worker_id: str | None = None,
        *,
        stop_requested: Callable[[], bool] = lambda: False,
    ) -> WorkerOutcome:
        worker_id = worker_id or f"worker-{secrets.token_hex(8)}"
        parent, child = self._context.Pipe(duplex=True)
        process = self._context.Process(
            target=_worker_main,
            args=(self._factory, worker_id, self._policy.lease_seconds, child),
            daemon=False,
        )
        try:
            process.start()
        except BaseException as exc:
            parent.close()
            child.close()
            pid = process.pid
            cleaned = True
            if pid is not None:
                ticks = _process_start_ticks(pid)
                cleaned = self._finish_process(process, pid, ticks if ticks is not None else -1)
            if not isinstance(exc, Exception):
                raise
            return WorkerOutcome("worker_start_failed", worker_id, None, None, cleaned)
        child.close()
        pid = process.pid
        if pid is None:
            parent.close()
            return WorkerOutcome("worker_start_failed", worker_id, None, None, False)
        start_ticks = _process_start_ticks(pid)
        if start_ticks is None:
            cleaned = self._finish_process(process, pid, -1)
            parent.close()
            return WorkerOutcome("worker_start_failed", worker_id, None, None, cleaned)

        task_id: str | None = None
        task_state: str | None = None
        status = "worker_crashed"
        terminal = False
        waiting = False
        command_sent = False
        store: CollectionTaskStore | None = None
        deadline = time.monotonic() + self._policy.startup_timeout_seconds
        next_heartbeat = time.monotonic() + self._policy.heartbeat_seconds
        interrupted: BaseException | None = None

        try:
            while not terminal:
                if stop_requested():
                    status = "worker_stopped"
                    break
                now = time.monotonic()
                if now >= deadline:
                    status = (
                        "worker_manual_timeout"
                        if waiting
                        else ("worker_start_timeout" if task_id is None else "worker_timed_out")
                    )
                    break
                if parent.poll(min(0.05, max(0.0, deadline - now))):
                    try:
                        message = parent.recv()
                    except EOFError:
                        status = "worker_crashed"
                        break
                    if not isinstance(message, dict):
                        status = "worker_protocol_error"
                        break
                    kind = message.get("kind")
                    if kind == "ready" and task_id is None:
                        parent.send({"command": "start"})
                    elif kind == "idle" and task_id is None:
                        status = "idle"
                        terminal = True
                    elif kind == "claimed" and task_id is None:
                        value = message.get("task_id")
                        if not isinstance(value, str):
                            status = "worker_protocol_error"
                            break
                        task_id = value
                        store = CollectionTaskStore(
                            self._database,
                            sqlite_timeout_seconds=_SUPERVISOR_SQLITE_TIMEOUT_SECONDS,
                        )
                        wall_now = self._clock()
                        wall_deadline = wall_now + timedelta(seconds=self._policy.execution_timeout_seconds)
                        store.attach_worker_process(
                            task_id,
                            worker_id,
                            pid,
                            start_ticks,
                            wall_deadline,
                            wall_now,
                        )
                        deadline = time.monotonic() + self._policy.execution_timeout_seconds
                        next_heartbeat = time.monotonic() + self._policy.heartbeat_seconds
                        parent.send({"command": "execute"})
                    elif kind == "waiting" and message.get("task_id") == task_id and store is not None:
                        waiting = True
                        command_sent = False
                        wall_now = self._clock()
                        wall_deadline = wall_now + timedelta(seconds=self._policy.manual_timeout_seconds)
                        store.update_worker_deadline(
                            task_id,
                            worker_id,
                            pid,
                            start_ticks,
                            wall_deadline,
                            wall_now,
                        )
                        deadline = time.monotonic() + self._policy.manual_timeout_seconds
                    elif kind == "result" and message.get("task_id") == task_id:
                        value = message.get("state")
                        if value not in {item.value for item in TaskState}:
                            status = "worker_protocol_error"
                            break
                        task_state = value
                        status = value
                        terminal = True
                    elif kind == "error":
                        if task_id is not None and store is not None:
                            current = store.get(task_id)
                            if current.state not in {TaskState.RUNNING, TaskState.WAITING_FOR_USER}:
                                task_state = current.state.value
                                status = current.state.value
                                terminal = True
                                continue
                        status = "collection_worker_failed"
                        break
                    else:
                        status = "worker_protocol_error"
                        break

                if task_id is not None and store is not None:
                    now = time.monotonic()
                    if now >= next_heartbeat:
                        try:
                            current = store.heartbeat(
                                task_id, worker_id, self._clock(), self._policy.lease_seconds
                            )
                        except CollectionStoreError:
                            current = store.get(task_id)
                            if (
                                current.worker_id == worker_id
                                and current.worker_pid == pid
                                and current.worker_start_ticks == start_ticks
                                and current.state not in {TaskState.RUNNING, TaskState.WAITING_FOR_USER}
                            ):
                                task_state = current.state.value
                                status = current.state.value
                                terminal = True
                                continue
                            raise
                        next_heartbeat = now + self._policy.heartbeat_seconds
                        if waiting and not command_sent:
                            if current.cancel_requested:
                                parent.send({"command": "cancel"})
                                command_sent = True
                            elif current.resume_requested:
                                wall_now = self._clock()
                                wall_deadline = wall_now + timedelta(seconds=self._policy.execution_timeout_seconds)
                                store.update_worker_deadline(
                                    task_id,
                                    worker_id,
                                    pid,
                                    start_ticks,
                                    wall_deadline,
                                    wall_now,
                                )
                                deadline = time.monotonic() + self._policy.execution_timeout_seconds
                                waiting = False
                                parent.send({"command": "resume"})
                                command_sent = True
        except (BrokenPipeError, EOFError):
            status = "worker_crashed"
        except (OSError, CollectionStoreError):
            status = "worker_state_unavailable"
        except Exception:
            status = "worker_state_unavailable"
        except BaseException as exc:
            interrupted = exc
            status = "worker_interrupted"
        else:
            interrupted = None
        finally:
            try:
                parent.close()
            except OSError:
                pass
            try:
                cleaned = self._finish_process(process, pid, start_ticks)
            except Exception:
                cleaned = False
            if task_id is not None and cleaned:
                try:
                    store = store or CollectionTaskStore(
                        self._database,
                        sqlite_timeout_seconds=_SUPERVISOR_SQLITE_TIMEOUT_SECONDS,
                    )
                    current = store.get(task_id)
                    if (
                        current.worker_pid == pid
                        and current.worker_start_ticks == start_ticks
                        and current.worker_id == worker_id
                    ):
                        if current.state in {TaskState.RUNNING, TaskState.WAITING_FOR_USER}:
                            changed = store.reconcile_worker(worker_id, pid, start_ticks, self._clock())
                            if changed:
                                current = changed[0]
                                task_state = current.state.value
                        else:
                            current = store.clear_worker_process(
                                task_id, worker_id, pid, start_ticks, self._clock()
                            )
                            task_state = current.state.value
                except Exception:
                    status = "worker_state_unavailable"
            elif task_id is not None and not cleaned:
                status = "worker_cleanup_failed"
        if interrupted is not None:
            raise interrupted
        return WorkerOutcome(status, worker_id, task_id, task_state, cleaned)

    def reconcile_orphans(self) -> list[TaskRecord]:
        store = CollectionTaskStore(self._database, sqlite_timeout_seconds=0.0)
        changed: list[TaskRecord] = []
        now = self._clock()
        for task in store.active_process_tasks():
            pid = task.worker_pid
            ticks = task.worker_start_ticks
            worker_id = task.worker_id
            if pid is None or ticks is None or worker_id is None:
                continue
            active = task.state in {TaskState.RUNNING, TaskState.WAITING_FOR_USER}
            deadline_live = task.worker_deadline_at is not None and task.worker_deadline_at > now
            lease_live = task.lease_expires_at is not None and task.lease_expires_at > now
            if deadline_live and (not active or lease_live):
                continue
            observed = _process_start_ticks(pid)
            cleaned = observed is None and not _group_has_live_members(pid)
            if observed == ticks:
                cleaned = _stop_group(pid, ticks, self._policy.termination_grace_seconds)
            if not cleaned:
                continue
            current = store.get(task.task_id)
            if current.state in {TaskState.RUNNING, TaskState.WAITING_FOR_USER}:
                changed.extend(store.reconcile_worker(worker_id, pid, ticks, now))
            else:
                changed.append(store.clear_worker_process(task.task_id, worker_id, pid, ticks, now))
        return changed

    def _finish_process(self, process: multiprocessing.Process, pid: int, start_ticks: int) -> bool:
        if start_ticks >= 0 and _process_start_ticks(pid) == start_ticks:
            try:
                owned_group = os.getpgid(pid) == pid
            except ProcessLookupError:
                owned_group = False
            if owned_group:
                try:
                    os.killpg(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                deadline = time.monotonic() + self._policy.termination_grace_seconds
                while time.monotonic() < deadline and _process_state(pid) not in {None, "Z"}:
                    time.sleep(0.01)
                if _process_start_ticks(pid) == start_ticks:
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        process.join(timeout=self._policy.termination_grace_seconds)
        if process.is_alive():
            process.kill()
            process.join(timeout=self._policy.termination_grace_seconds)
        deadline = time.monotonic() + self._policy.termination_grace_seconds
        while time.monotonic() < deadline and _group_has_live_members(pid):
            time.sleep(0.01)
        return not process.is_alive() and not _group_has_live_members(pid)


@dataclass(frozen=True)
class ServiceTick:
    scheduled: int
    reconciled: int
    worker: WorkerOutcome

    def safe_data(self) -> dict[str, object]:
        return {
            "scheduled": self.scheduled,
            "reconciled": self.reconciled,
            "worker": self.worker.safe_data(),
        }


class CollectionService:
    """A foreground, stoppable service loop with no hidden worker threads."""

    def __init__(
        self,
        store: CollectionTaskStore,
        supervisor: CollectionWorkerSupervisor,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._store = store
        self._supervisor = supervisor
        self._clock = clock

    def tick(self, stop_requested: Callable[[], bool] = lambda: False) -> ServiceTick:
        orphaned = self._supervisor.reconcile_orphans()
        reconciled = orphaned + self._store.reconcile(self._clock())
        scheduled = self._store.enqueue_due(self._clock())
        worker = self._supervisor.run_once(stop_requested=stop_requested)
        return ServiceTick(len(scheduled), len(reconciled), worker)

    def run(
        self,
        stop_requested: Callable[[], bool],
        *,
        idle_seconds: float = 1.0,
        max_ticks: int | None = None,
    ) -> list[ServiceTick]:
        if not 0.05 <= idle_seconds <= 60:
            raise ValueError("service idle interval is invalid")
        results: deque[ServiceTick] = deque(maxlen=100)
        ticks = 0
        while not stop_requested() and (max_ticks is None or ticks < max_ticks):
            result = self.tick(stop_requested)
            results.append(result)
            ticks += 1
            if (
                result.worker.status == "idle"
                and not stop_requested()
                and (max_ticks is None or ticks < max_ticks)
            ):
                time.sleep(idle_seconds)
        return list(results)


__all__ = [
    "CollectionService",
    "CollectionWorkerSupervisor",
    "ManagerFactory",
    "ProductionManagerFactory",
    "ServiceTick",
    "WorkerOutcome",
    "WorkerPolicy",
]
