from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time

import pytest

from dispatch_core.collection_manager import (
    CollectionContext,
    CollectionDisposition,
    CollectionManager,
    CollectionReceipt,
    CollectorRegistration,
    TaskState,
)
from dispatch_core.collection_manager.queue import CollectionTaskStore
from dispatch_core.collection_manager.supervisor import (
    CollectionService,
    CollectionWorkerSupervisor,
    WorkerPolicy,
    _process_start_ticks,
)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _success(_context: CollectionContext) -> CollectionReceipt:
    return CollectionReceipt(CollectionDisposition.PUBLISHED, "supervised-1", 1, True)


def _sleep(_context: CollectionContext) -> CollectionReceipt:
    time.sleep(5)
    return CollectionReceipt(CollectionDisposition.PUBLISHED, "late-1", 1, True)


def _slow_success(_context: CollectionContext) -> CollectionReceipt:
    time.sleep(0.25)
    return CollectionReceipt(CollectionDisposition.PUBLISHED, "supervised-slow-1", 1, True)


def _crash(_context: CollectionContext) -> CollectionReceipt:
    os._exit(17)


def _secret_error(_context: CollectionContext) -> CollectionReceipt:
    raise SystemExit("secret-token-value")


def _tree(context: CollectionContext) -> CollectionReceipt:
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)",
        ]
    )
    Path(str(context.parameters["pid-file"])).write_text(str(child.pid), encoding="utf-8")
    time.sleep(30)
    return CollectionReceipt(CollectionDisposition.PUBLISHED, "late-tree-1", 1, True)


class _CrashBeforeExecutionManager(CollectionManager):
    def run_claimed(self, task_id: str, worker_id: str):
        os._exit(18)


class _ManualManager(CollectionManager):
    def run_claimed(self, task_id: str, worker_id: str):
        return self._require_store().wait_for_user(task_id, worker_id, "mfa_required", self._clock())

    def resume_task(self, task_id: str, lease_seconds: int = 900):
        store = self._require_store()
        current = store.get(task_id)
        resumed = store.resume_waiting(task_id, current.worker_id, self._clock(), lease_seconds)
        store.mark_execution_started(task_id, resumed.worker_id, self._clock())
        return store.finish(
            task_id,
            resumed.worker_id,
            TaskState.SUCCEEDED,
            self._clock(),
            receipt=CollectionReceipt(
                CollectionDisposition.PUBLISHED, "manual-1", 1, True
            ).safe_data(),
        )


@dataclass(frozen=True)
class _Factory:
    database: str
    mode: str

    def __call__(self) -> CollectionManager:
        store = CollectionTaskStore(Path(self.database))
        manager_type = (
            _CrashBeforeExecutionManager
            if self.mode == "crash-before"
            else (_ManualManager if self.mode == "manual" else CollectionManager)
        )
        manager = manager_type(store=store)
        runners = {
            "success": _success,
            "sleep": _sleep,
            "slow": _slow_success,
            "crash": _crash,
            "secret": _secret_error,
            "tree": _tree,
            "crash-before": _success,
            "manual": _success,
        }
        if self.mode != "idle":
            manager.register(CollectorRegistration("test-collector", "test-plugin", "1.0.0", runners[self.mode]))
        return manager


def _store(tmp_path: Path) -> CollectionTaskStore:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    return CollectionTaskStore(root / "queue.sqlite3")


def _policy(*, timeout: float = 2.0) -> WorkerPolicy:
    return WorkerPolicy(
        lease_seconds=30,
        heartbeat_seconds=0.05,
        startup_timeout_seconds=5,
        execution_timeout_seconds=timeout,
        manual_timeout_seconds=2,
        termination_grace_seconds=0.1,
    )


def _enqueue(store: CollectionTaskStore, parameters: dict[str, object] | None = None) -> str:
    return store.enqueue(
        collector_id="test-collector",
        account_alias="default",
        parameters=parameters or {},
        max_attempts=3,
        at=NOW,
    ).task_id


def _supervisor(store: CollectionTaskStore, mode: str, *, timeout: float = 2.0) -> CollectionWorkerSupervisor:
    return CollectionWorkerSupervisor(
        store.database,
        _Factory(str(store.database), mode),
        _policy(timeout=timeout),
    )


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def test_supervised_worker_completes_and_clears_durable_process_identity(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _enqueue(store)

    outcome = _supervisor(store, "success").run_once("worker-success")
    task = store.get(task_id)

    assert outcome.status == "succeeded"
    assert outcome.process_cleaned is True
    assert task.state == TaskState.SUCCEEDED
    assert task.worker_pid is task.worker_start_ticks is task.worker_deadline_at is None
    assert task.worker_id is None


def test_worker_crash_before_execution_watermark_retries(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _enqueue(store)

    outcome = _supervisor(store, "crash-before").run_once("worker-before")
    task = store.get(task_id)

    assert outcome.status == "worker_crashed"
    assert outcome.process_cleaned is True
    assert task.state == TaskState.RETRY_WAIT
    assert task.last_error_code == "worker_interrupted"


def test_worker_crash_after_execution_watermark_becomes_uncertain(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _enqueue(store)

    outcome = _supervisor(store, "crash").run_once("worker-after")
    task = store.get(task_id)

    assert outcome.status == "worker_crashed"
    assert outcome.process_cleaned is True
    assert task.state == TaskState.UNCERTAIN
    assert task.last_error_code == "worker_interrupted_after_execution"


def test_worker_exception_status_uses_closed_vocabulary(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _enqueue(store)

    outcome = _supervisor(store, "secret").run_once("worker-secret")

    assert outcome.status == "collection_worker_failed"
    assert "secret-token-value" not in repr(outcome.safe_data())
    assert store.get(task_id).state == TaskState.UNCERTAIN


def test_supervisor_rejects_fork_context(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(ValueError, match="spawn"):
        CollectionWorkerSupervisor(
            store.database,
            _Factory(str(store.database), "idle"),
            _policy(),
            context="fork",
        )


def test_store_lock_error_still_stops_worker_without_waiting_for_busy_timeout(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _enqueue(store)
    lock: sqlite3.Connection | None = None

    def lock_after_process_attachment() -> bool:
        nonlocal lock
        if lock is None and store.get(task_id).execution_started:
            lock = sqlite3.connect(store.database, timeout=0)
            lock.execute("BEGIN IMMEDIATE")
        return False

    started = time.monotonic()
    outcome = _supervisor(store, "sleep", timeout=2).run_once(
        "worker-locked",
        stop_requested=lock_after_process_attachment,
    )
    elapsed = time.monotonic() - started
    assert lock is not None
    try:
        assert outcome.status == "worker_state_unavailable"
        assert outcome.process_cleaned is True
        assert elapsed < 1.5
        assert store.get(task_id).worker_pid is not None
    finally:
        lock.rollback()
        lock.close()

    future = datetime.now(timezone.utc).replace(year=datetime.now(timezone.utc).year + 1)
    reconciler = CollectionWorkerSupervisor(
        store.database,
        _Factory(str(store.database), "idle"),
        _policy(),
        clock=lambda: future,
    )
    changed = reconciler.reconcile_orphans()
    assert [task.task_id for task in changed] == [task_id]
    assert store.get(task_id).state == TaskState.UNCERTAIN


def test_keyboard_interrupt_still_cleans_and_reconciles_worker(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _enqueue(store)

    def interrupt_after_process_attachment() -> bool:
        if store.get(task_id).execution_started:
            raise KeyboardInterrupt
        return False

    with pytest.raises(KeyboardInterrupt):
        _supervisor(store, "sleep", timeout=2).run_once(
            "worker-interrupted",
            stop_requested=interrupt_after_process_attachment,
        )

    current = store.get(task_id)
    assert current.state == TaskState.UNCERTAIN
    assert current.worker_pid is None


def test_heartbeats_do_not_prevent_hard_stop_request(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _enqueue(store)
    execution_seen_at: float | None = None

    def stop_after_execution() -> bool:
        nonlocal execution_seen_at
        if store.get(task_id).execution_started and execution_seen_at is None:
            execution_seen_at = time.monotonic()
        return execution_seen_at is not None and time.monotonic() - execution_seen_at >= 0.2

    outcome = _supervisor(store, "sleep").run_once(
        "worker-stop",
        stop_requested=stop_after_execution,
    )

    assert outcome.status == "worker_stopped"
    assert outcome.process_cleaned is True
    assert store.get(task_id).state == TaskState.UNCERTAIN


def test_slow_worker_is_heartbeated_without_sliding_execution_deadline(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _enqueue(store)
    calls = 0

    def clock() -> datetime:
        nonlocal calls
        calls += 1
        return NOW

    supervisor = CollectionWorkerSupervisor(
        store.database,
        _Factory(str(store.database), "slow"),
        _policy(timeout=1),
        clock=clock,
    )
    outcome = supervisor.run_once("worker-heartbeat")

    assert outcome.status == "succeeded"
    assert store.get(task_id).state == TaskState.SUCCEEDED
    assert calls >= 3


def test_durable_manual_resume_reaches_the_same_owning_worker(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _enqueue(store)
    requested = False

    def request_resume_when_waiting() -> bool:
        nonlocal requested
        current = store.get(task_id)
        if current.state == TaskState.WAITING_FOR_USER and not requested:
            store.request_resume(task_id, datetime.now(timezone.utc))
            requested = True
        return False

    outcome = _supervisor(store, "manual").run_once(
        "worker-manual",
        stop_requested=request_resume_when_waiting,
    )

    assert requested is True
    assert outcome.status == "succeeded"
    assert store.get(task_id).state == TaskState.SUCCEEDED


def test_process_deadline_terminates_owned_tree_before_reconciliation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    pid_file = tmp_path / "descendant.pid"
    task_id = _enqueue(store, {"pid-file": str(pid_file)})

    outcome = _supervisor(store, "tree", timeout=0.3).run_once("worker-timeout")
    task = store.get(task_id)
    descendant = int(pid_file.read_text(encoding="utf-8"))

    assert outcome.status == "worker_timed_out"
    assert outcome.process_cleaned is True
    assert task.state == TaskState.UNCERTAIN
    deadline = time.monotonic() + 2
    while _pid_exists(descendant) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert _pid_exists(descendant) is False


def test_idle_worker_does_not_claim_unregistered_work(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _enqueue(store)

    outcome = _supervisor(store, "idle").run_once("worker-idle")

    assert outcome.status == "idle"
    assert store.get(task_id).state == TaskState.QUEUED


def test_startup_reconciliation_stops_durably_owned_orphan_before_retry(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _enqueue(store)
    claimed = store.claim("worker-orphan", NOW, 30)
    assert claimed is not None
    process = subprocess.Popen(
        [sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"],
        start_new_session=True,
    )
    ticks = _process_start_ticks(process.pid)
    assert ticks is not None
    store.attach_worker_process(
        task_id,
        "worker-orphan",
        process.pid,
        ticks,
        NOW.replace(minute=5),
        NOW,
    )

    try:
        changed = _supervisor(store, "idle").reconcile_orphans()
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=2)

    assert [item.task_id for item in changed] == [task_id]
    assert store.get(task_id).state == TaskState.RETRY_WAIT
    assert _pid_exists(process.pid) is False


def test_reconciliation_quarantines_missing_leader_with_live_group_member(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _enqueue(store)
    claimed = store.claim("worker-missing-leader", NOW, 30)
    assert claimed is not None
    child_file = tmp_path / "orphan-child.pid"
    program = (
        "import os,pathlib,time; "
        "child=os.fork(); "
        f"path=pathlib.Path({str(child_file)!r}); "
        "(path.write_text(str(child)) if child else None); "
        "time.sleep(0.3 if child else 30); "
        "os._exit(0)"
    )
    leader = subprocess.Popen([sys.executable, "-c", program], start_new_session=True)
    ticks = _process_start_ticks(leader.pid)
    assert ticks is not None
    deadline = time.monotonic() + 2
    while not child_file.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert child_file.exists()
    child_pid = int(child_file.read_text(encoding="utf-8"))
    store.attach_worker_process(
        task_id,
        "worker-missing-leader",
        leader.pid,
        ticks,
        NOW.replace(minute=5),
        NOW,
    )
    leader.wait(timeout=2)

    try:
        changed = _supervisor(store, "idle").reconcile_orphans()
        current = store.get(task_id)
        assert changed == []
        assert current.state == TaskState.RUNNING
        assert current.worker_pid == leader.pid
        assert _pid_exists(child_pid) is True
    finally:
        try:
            os.kill(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_reconciliation_does_not_kill_a_live_leased_worker(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _enqueue(store)
    claimed = store.claim("worker-live", NOW, 30)
    assert claimed is not None
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    ticks = _process_start_ticks(process.pid)
    assert ticks is not None
    store.attach_worker_process(
        task_id,
        "worker-live",
        process.pid,
        ticks,
        NOW.replace(minute=5),
        NOW,
    )
    supervisor = CollectionWorkerSupervisor(
        store.database,
        _Factory(str(store.database), "idle"),
        _policy(),
        clock=lambda: NOW,
    )

    try:
        assert supervisor.reconcile_orphans() == []
        assert _pid_exists(process.pid) is True
        assert store.get(task_id).state == TaskState.RUNNING
    finally:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=2)


def test_service_loop_is_foreground_and_stops_at_requested_bound(tmp_path: Path) -> None:
    store = _store(tmp_path)
    supervisor = _supervisor(store, "idle")
    service = CollectionService(store, supervisor)

    results = service.run(lambda: False, idle_seconds=0.05, max_ticks=1)

    assert len(results) == 1
    assert results[0].worker.status == "idle"
