from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import pytest

from dispatch_core.collection_manager import (
    CollectionDisposition,
    CollectionManager,
    CollectionManagerError,
    CollectionReceipt,
    CollectionRequest,
    CollectionStoreError,
    CollectionTaskStore,
    CollectorRegistration,
    PublicationVerification,
    TaskState,
)
from dispatch_core.paths import DispatchPaths


NOW = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[3]


@dataclass
class Clock:
    value: datetime = NOW

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


@pytest.fixture
def store(tmp_path) -> CollectionTaskStore:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    return CollectionTaskStore(root / "collection.sqlite3")


def enqueue(store: CollectionTaskStore, **values):
    return store.enqueue(
        collector_id=values.pop("collector_id", "synthetic-collector"),
        account_alias="default",
        parameters=values.pop("parameters", {}),
        max_attempts=values.pop("max_attempts", 3),
        at=values.pop("at", NOW),
        **values,
    )


def safe_receipt() -> dict[str, object]:
    return {
        "disposition": "published",
        "publication_id": "synthetic-publication-1",
        "artifact_count": 1,
        "domain_complete": True,
    }


def test_enqueue_is_idempotent_and_claim_is_transactional(store: CollectionTaskStore) -> None:
    first = enqueue(store, idempotency_key="request-1", parameters={"date": "2026-08-14"})
    repeated = enqueue(store, idempotency_key="request-1", parameters={"date": "2026-08-14"})
    second = enqueue(store, idempotency_key="request-2")

    assert repeated.task_id == first.task_id
    with pytest.raises(CollectionStoreError) as conflict:
        enqueue(store, idempotency_key="request-1", parameters={"date": "2026-08-15"})
    assert conflict.value.code == "collection_idempotency_conflict"

    other_process = CollectionTaskStore(store.database)
    claimed_first = store.claim("worker-one", NOW, 30)
    claimed_second = other_process.claim("worker-two", NOW, 30)

    assert {claimed_first.task_id, claimed_second.task_id} == {first.task_id, second.task_id}
    assert claimed_first.worker_id != claimed_second.worker_id
    assert store.claim("worker-three", NOW, 30) is None

    past_due = enqueue(store, not_before=NOW - timedelta(seconds=1))
    assert past_due.state == TaskState.QUEUED
    assert past_due.not_before is None


def test_reconciliation_retries_only_before_execution(store: CollectionTaskStore) -> None:
    retryable = enqueue(store)
    store.claim("worker-one", NOW, 30)

    reconciled = store.reconcile(NOW + timedelta(seconds=31))[0]
    assert reconciled.task_id == retryable.task_id
    assert reconciled.state == TaskState.RETRY_WAIT
    assert reconciled.not_before == NOW + timedelta(seconds=61)
    assert store.claim("worker-two", NOW + timedelta(seconds=60), 30) is None

    claimed_again = store.claim("worker-two", NOW + timedelta(seconds=61), 30)
    store.mark_execution_started(claimed_again.task_id, "worker-two", NOW + timedelta(seconds=61))
    uncertain = store.reconcile(NOW + timedelta(seconds=92))[0]

    assert uncertain.state == TaskState.UNCERTAIN
    assert uncertain.last_error_code == "worker_interrupted_after_execution"
    with pytest.raises(CollectionStoreError) as confirmation:
        store.retry(uncertain.task_id, NOW + timedelta(seconds=93))
    assert confirmation.value.code == "publication_verification_required"
    unverified = CollectionManager(store=store, clock=lambda: NOW + timedelta(seconds=93))
    unverified.register(
        CollectorRegistration(
            "synthetic-collector",
            "synthetic-plugin",
            "1.0.0",
            lambda context: safe_receipt(),
            publication_verifier=lambda request, receipt: True,
        )
    )
    with pytest.raises(CollectionManagerError) as rejected:
        unverified.retry_task(uncertain.task_id)
    assert rejected.value.code == "publication_verification_failed"

    manager = CollectionManager(store=store, clock=lambda: NOW + timedelta(seconds=93))
    manager.register(
        CollectorRegistration(
            "synthetic-collector",
            "synthetic-plugin",
            "1.0.0",
            lambda context: safe_receipt(),
            publication_verifier=lambda request, receipt: PublicationVerification.ABSENT,
        )
    )
    retried = manager.retry_task(uncertain.task_id)
    assert retried.state == TaskState.QUEUED
    assert retried.publication_absence_verified_at == NOW + timedelta(seconds=93)
    assert retried.verified_absent_publication_id is None
    assert CollectionTaskStore(store.database).get(uncertain.task_id).publication_absence_verified_at == (
        NOW + timedelta(seconds=93)
    )


def test_cancellation_prevents_execution_from_starting(store: CollectionTaskStore) -> None:
    task = enqueue(store)
    claimed = store.claim("worker-one", NOW, 30)
    assert claimed.task_id == task.task_id
    store.request_cancel(task.task_id, NOW + timedelta(seconds=1))

    with pytest.raises(CollectionStoreError) as conflict:
        store.mark_execution_started(task.task_id, "worker-one", NOW + timedelta(seconds=2))

    assert conflict.value.code == "collection_state_conflict"
    current = store.get(task.task_id)
    assert current.cancel_requested is True
    assert current.execution_started is False


def test_process_owned_task_requires_verified_cleanup_before_reuse(store: CollectionTaskStore) -> None:
    task = enqueue(store)
    store.claim("worker-one", NOW, 30)
    attached = store.attach_worker_process(
        task.task_id,
        "worker-one",
        4242,
        99,
        NOW + timedelta(minutes=5),
        NOW,
    )

    assert attached.worker_pid == 4242
    assert store.reconcile(NOW + timedelta(seconds=31)) == []

    store.mark_execution_started(task.task_id, "worker-one", NOW)
    finished = store.finish(
        task.task_id,
        "worker-one",
        TaskState.SUCCEEDED,
        NOW + timedelta(seconds=1),
        receipt=safe_receipt(),
    )
    assert finished.worker_pid == 4242
    assert finished.worker_id == "worker-one"

    cleared = store.clear_worker_process(task.task_id, "worker-one", 4242, 99, NOW + timedelta(seconds=2))
    assert cleared.worker_pid is None
    assert cleared.worker_id is None


def test_read_only_inspection_reports_overdue_worker_without_exposing_identity(tmp_path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    paths = DispatchPaths.from_environment({}, home=home, code_root=ROOT)
    store = CollectionTaskStore.from_paths(paths)
    inspected_at = datetime.now(timezone.utc)
    task = enqueue(store, at=inspected_at - timedelta(minutes=2))
    store.claim("worker-overdue", inspected_at - timedelta(minutes=2), 30)
    store.attach_worker_process(
        task.task_id,
        "worker-overdue",
        4242,
        99,
        inspected_at - timedelta(seconds=1),
        inspected_at - timedelta(minutes=2),
    )

    status = CollectionTaskStore.inspect_paths(paths)

    assert status["ready"] is False
    assert status["status"] == "reconciliation_required"
    assert status["workers"] == 1
    assert status["overdue_workers"] == 1
    assert "4242" not in repr(status)


def test_manual_resume_request_is_durable_and_consumed_by_owner(store: CollectionTaskStore) -> None:
    task = enqueue(store)
    store.claim("worker-one", NOW, 30)
    waiting = store.wait_for_user(task.task_id, "worker-one", "mfa_required", NOW)

    requested = store.request_resume(task.task_id, NOW + timedelta(seconds=1))
    assert requested.resume_requested is True
    assert CollectionTaskStore(store.database).get(task.task_id).resume_requested is True

    resumed = store.resume_waiting(task.task_id, "worker-one", NOW + timedelta(seconds=2), 30)
    assert resumed.state == TaskState.RUNNING
    assert resumed.resume_requested is False


def test_invalid_finish_does_not_commit_corrupt_state(store: CollectionTaskStore) -> None:
    task = enqueue(store)
    store.claim("worker-one", NOW, 30)

    with pytest.raises(CollectionStoreError) as invalid:
        store.finish(task.task_id, "worker-one", TaskState.CANCELLED, NOW + timedelta(seconds=1))

    assert invalid.value.code == "invalid_collection_transition"
    assert store.get(task.task_id).state == TaskState.RUNNING


def test_cancellation_never_requeues_started_work(store: CollectionTaskStore) -> None:
    queued = enqueue(store)
    assert store.request_cancel(queued.task_id, NOW).state == TaskState.CANCELLED
    assert store.retry(queued.task_id, NOW + timedelta(seconds=1)).state == TaskState.QUEUED

    running = store.claim("worker-one", NOW + timedelta(seconds=1), 30)
    requested = store.request_cancel(running.task_id, NOW + timedelta(seconds=2))
    assert requested.state == TaskState.RUNNING
    assert requested.cancel_requested is True
    assert store.finish(
        running.task_id,
        "worker-one",
        TaskState.CANCELLED,
        NOW + timedelta(seconds=3),
        error_code="cancelled",
    ).state == TaskState.CANCELLED

    started = enqueue(store)
    started = store.claim("worker-two", NOW + timedelta(seconds=4), 30)
    store.mark_execution_started(started.task_id, "worker-two", NOW + timedelta(seconds=4))
    store.request_cancel(started.task_id, NOW + timedelta(seconds=5))
    with pytest.raises(CollectionStoreError):
        store.finish(
            started.task_id,
            "worker-two",
            TaskState.CANCELLED,
            NOW + timedelta(seconds=6),
            error_code="cancelled",
        )
    assert store.reconcile(NOW + timedelta(seconds=35))[0].state == TaskState.UNCERTAIN


def test_failed_published_work_requires_verification_before_retry(store: CollectionTaskStore) -> None:
    task = enqueue(store)
    store.claim("worker-one", NOW, 30)
    store.mark_execution_started(task.task_id, "worker-one", NOW)
    failed = store.finish(
        task.task_id,
        "worker-one",
        TaskState.FAILED,
        NOW + timedelta(seconds=1),
        error_code="browser_cleanup_failed",
        receipt=safe_receipt(),
    )

    with pytest.raises(CollectionStoreError) as confirmation:
        store.retry(failed.task_id, NOW + timedelta(seconds=2))
    assert confirmation.value.code == "publication_verification_required"
    manager = CollectionManager(store=store, clock=lambda: NOW + timedelta(seconds=2))
    manager.register(
        CollectorRegistration(
            "synthetic-collector",
            "synthetic-plugin",
            "1.0.0",
            lambda context: safe_receipt(),
            publication_verifier=lambda request, receipt: PublicationVerification.ABSENT,
        )
    )
    retried = manager.retry_task(failed.task_id)
    assert retried.state == TaskState.QUEUED
    assert retried.publication_absence_verified_at == NOW + timedelta(seconds=2)
    assert retried.verified_absent_publication_id == "synthetic-publication-1"
    persisted = CollectionTaskStore(store.database).get(failed.task_id)
    assert persisted.publication_absence_verified_at == NOW + timedelta(seconds=2)
    assert persisted.verified_absent_publication_id == "synthetic-publication-1"


def test_schedule_enqueues_one_durable_occurrence_and_coalesces_missed_intervals(
    store: CollectionTaskStore,
) -> None:
    schedule = store.create_schedule(
        collector_id="synthetic-collector",
        account_alias="default",
        parameters={"replace": False},
        interval_seconds=60,
        next_run_at=NOW,
        max_attempts=3,
        at=NOW,
    )

    first = store.enqueue_due(NOW)
    assert len(first) == 1
    assert first[0].schedule_id == schedule.schedule_id
    assert store.enqueue_due(NOW) == []

    later = store.enqueue_due(NOW + timedelta(seconds=185))
    assert len(later) == 1
    assert len(store.recent()) == 2
    assert store.get_schedule(schedule.schedule_id).next_run_at == NOW + timedelta(seconds=240)

    store.set_schedule_enabled(schedule.schedule_id, False, NOW + timedelta(seconds=186))
    assert store.enqueue_due(NOW + timedelta(seconds=300)) == []


def test_manager_persists_success_transient_retry_and_uncertain_runner_failure(
    store: CollectionTaskStore,
) -> None:
    clock = Clock()
    manager = CollectionManager(store=store, clock=clock)
    manager.register(
        CollectorRegistration(
            "success-collector",
            "synthetic-plugin",
            "1.0.0",
            lambda context: CollectionReceipt(
                CollectionDisposition.PUBLISHED,
                "synthetic-publication-1",
                1,
                True,
            ),
        )
    )
    successful = manager.enqueue(CollectionRequest("success-collector"))
    completed = manager.run_next("worker-one", 30)

    assert completed.task_id == successful.task_id
    assert completed.state == TaskState.SUCCEEDED
    assert completed.receipt == safe_receipt()

    manager.register(
        CollectorRegistration(
            "deferred-collector",
            "synthetic-plugin",
            "1.0.0",
            lambda context: safe_receipt(),
            browser_realm="amazon-operations",
        )
    )
    deferred = manager.enqueue(CollectionRequest("deferred-collector"), max_attempts=2)
    first_attempt = manager.run_next("worker-one", 30)
    assert first_attempt.task_id == deferred.task_id
    assert first_attempt.state == TaskState.RETRY_WAIT
    assert first_attempt.last_error_code == "collection_dependencies_unavailable"

    clock.advance(30)
    assert manager.run_next("worker-one", 30).state == TaskState.FAILED

    def fail_after_start(context):
        raise RuntimeError("private-secret-marker")

    manager.register(
        CollectorRegistration("failing-collector", "synthetic-plugin", "1.0.0", fail_after_start)
    )
    failed = manager.enqueue(CollectionRequest("failing-collector"))
    uncertain = manager.run_next("worker-one", 30)
    assert uncertain.task_id == failed.task_id
    assert uncertain.state == TaskState.UNCERTAIN
    assert "private-secret-marker" not in repr(uncertain.safe_data())


def test_manager_schedule_key_is_stable_across_restart(store: CollectionTaskStore) -> None:
    manager = CollectionManager(store=store, clock=lambda: NOW)
    manager.register(
        CollectorRegistration("synthetic-collector", "synthetic-plugin", "1.0.0", lambda context: safe_receipt())
    )
    request = CollectionRequest("synthetic-collector")

    first = manager.create_schedule(
        request,
        schedule_key="daily-collection",
        interval_seconds=86_400,
        next_run_at=NOW,
    )
    repeated = manager.create_schedule(
        request,
        schedule_key="daily-collection",
        interval_seconds=86_400,
        next_run_at=NOW + timedelta(days=1),
    )

    assert repeated.schedule_id == first.schedule_id
    assert len(store.schedules()) == 1
    with pytest.raises(CollectionStoreError) as conflict:
        manager.create_schedule(
            request,
            schedule_key="daily-collection",
            interval_seconds=3600,
            next_run_at=NOW,
        )
    assert conflict.value.code == "collection_schedule_conflict"


def test_unknown_schema_and_nonprivate_parent_fail_closed(tmp_path) -> None:
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o755)
    with pytest.raises(CollectionStoreError) as private:
        CollectionTaskStore(unsafe / "collection.sqlite3")
    assert private.value.code == "unsafe_collection_storage"

    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    database = root / "collection.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    connection.execute("INSERT INTO metadata VALUES('schema_version','999')")
    connection.commit()
    connection.close()
    with pytest.raises(CollectionStoreError) as schema:
        CollectionTaskStore(database)
    assert schema.value.code == "unsupported_collection_schema"

    malformed_root = tmp_path / "malformed"
    malformed_root.mkdir(mode=0o700)
    malformed_database = malformed_root / "collection.sqlite3"
    CollectionTaskStore(malformed_database)
    connection = sqlite3.connect(malformed_database)
    connection.execute("ALTER TABLE tasks RENAME TO original_tasks")
    connection.execute("CREATE TABLE tasks AS SELECT * FROM original_tasks WHERE 0")
    connection.execute("DROP TABLE original_tasks")
    connection.commit()
    connection.close()
    with pytest.raises(CollectionStoreError) as malformed:
        CollectionTaskStore(malformed_database)
    assert malformed.value.code == "collection_state_corrupt"


@pytest.mark.parametrize(
    "statement",
    [
        "ALTER TABLE metadata ADD COLUMN unreviewed TEXT",
        (
            "CREATE TRIGGER unreviewed_trigger BEFORE INSERT ON tasks "
            "BEGIN SELECT RAISE(ABORT, 'blocked'); END"
        ),
    ],
)
def test_known_schema_version_rejects_unreviewed_schema_objects(tmp_path, statement: str) -> None:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    database = root / "collection.sqlite3"
    CollectionTaskStore(database)
    connection = sqlite3.connect(database)
    connection.execute(statement)
    connection.commit()
    connection.close()

    with pytest.raises(CollectionStoreError) as corrupt:
        CollectionTaskStore(database)
    assert corrupt.value.code == "collection_state_corrupt"


def test_unrelated_schedule_integrity_error_does_not_consume_occurrence(store: CollectionTaskStore) -> None:
    schedule = store.create_schedule(
        collector_id="synthetic-collector",
        account_alias="default",
        parameters={},
        interval_seconds=60,
        next_run_at=NOW,
        max_attempts=3,
        at=NOW,
    )
    connection = sqlite3.connect(store.database)
    connection.execute(
        "CREATE TRIGGER block_scheduled_insert BEFORE INSERT ON tasks "
        "BEGIN SELECT RAISE(ABORT, 'blocked'); END"
    )
    connection.commit()
    connection.close()

    with pytest.raises(CollectionStoreError) as failed:
        store.enqueue_due(NOW)
    assert failed.value.code == "collection_schedule_enqueue_failed"
    assert store.get_schedule(schedule.schedule_id).next_run_at == NOW
    assert store.recent() == []


def test_read_only_path_inspection_does_not_create_an_empty_store(tmp_path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    paths = DispatchPaths.from_environment(
        {},
        home=home,
        code_root=ROOT,
    )

    assert CollectionTaskStore.inspect_paths(paths) == {
        "ready": True,
        "status": "empty",
        "tasks": {},
        "schedules": 0,
        "workers": 0,
        "overdue_workers": 0,
    }
    assert not paths.data.exists()

    paths.data.mkdir(parents=True, mode=0o755)
    paths.data.chmod(0o755)
    with pytest.raises(CollectionStoreError) as unsafe:
        CollectionTaskStore.inspect_paths(paths)
    assert unsafe.value.code == "unsafe_collection_storage"
    assert not CollectionTaskStore.database_path(paths).exists()
    paths.data.chmod(0o700)

    CollectionTaskStore.from_paths(paths)
    inspected = CollectionTaskStore.inspect_paths(paths)
    assert inspected["ready"] is True
    assert inspected["status"] == "ready"
    assert inspected["schedules"] == 0


def test_read_only_path_inspection_rejects_symlinked_empty_data_root(tmp_path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    paths = DispatchPaths.from_environment({}, home=home, code_root=ROOT)
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    paths.data.parent.mkdir(parents=True, mode=0o700)
    paths.data.symlink_to(target, target_is_directory=True)

    with pytest.raises(CollectionStoreError) as unsafe:
        CollectionTaskStore.inspect_paths(paths)
    assert unsafe.value.code == "unsafe_collection_storage"
    assert list(target.iterdir()) == []
