from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from authentication import AuthenticationResult
from browser_manager import BrowserManagerError, BrowserMode, BrowserPurpose, LeaseState, ManagedBrowserSession
from collection_manager import (
    CollectionDisposition,
    CollectionManager,
    CollectionManagerError,
    CollectionReceipt,
    CollectionRequest,
    CollectionTaskStore,
    CollectorRegistration,
    TaskState,
)


@dataclass
class TerminalLease:
    state: LeaseState


class FakeLease:
    def __init__(self, realm: str, *, activation_error: bool = False) -> None:
        self.session = ManagedBrowserSession(
            lease_id="synthetic-lease",
            realm=realm,
            landing_url=(
                "https://logistics.amazon.com/dspconsolev2"
                if realm == "amazon-operations"
                else "https://www.paycomonline.net/v4/cl/web.php/client-landing/arc"
            ),
            page=object(),
            context=object(),
        )
        self.activated = False
        self.closed: str | None = None
        self.activation_error = activation_error

    def activate(self) -> TerminalLease:
        if self.activation_error:
            raise RuntimeError("synthetic activation failure")
        self.activated = True
        return TerminalLease(LeaseState.ACTIVE)

    def release(self) -> TerminalLease:
        self.closed = "released"
        return TerminalLease(LeaseState.CLOSED)

    def cancel(self) -> TerminalLease:
        self.closed = "cancelled"
        return TerminalLease(LeaseState.CANCELLED)


class FakeBrowserManager:
    def __init__(self, *, activation_error: bool = False) -> None:
        self.requests = []
        self.leases: list[FakeLease] = []
        self.activation_error = activation_error

    def acquire(self, request):
        self.requests.append(request)
        lease = FakeLease(request.realm, activation_error=self.activation_error)
        self.leases.append(lease)
        return lease


class FakeAuthentication:
    def __init__(self, *results: AuthenticationResult) -> None:
        self.results = list(results)
        self.calls: list[str] = []

    def authenticate(self, session, account_alias):
        self.calls.append(f"authenticate:{account_alias}")
        return self.results.pop(0)

    def resume(self, session, account_alias):
        self.calls.append(f"resume:{account_alias}")
        return self.results.pop(0)


def receipt() -> CollectionReceipt:
    return CollectionReceipt(
        disposition=CollectionDisposition.PUBLISHED,
        publication_id="synthetic-publication-1",
        artifact_count=1,
        domain_complete=True,
    )


@pytest.mark.parametrize(
    ("disposition", "publication_id", "artifact_count"),
    [
        (CollectionDisposition.PUBLISHED, "published-1", 0),
        (CollectionDisposition.SKIPPED_EXISTING, "published-1", 1),
        (CollectionDisposition.NO_DATA, "published-1", 0),
        (CollectionDisposition.NO_DATA, None, 1),
    ],
)
def test_receipt_disposition_must_match_publication_artifacts(
    disposition: CollectionDisposition,
    publication_id: str | None,
    artifact_count: int,
) -> None:
    with pytest.raises(CollectionManagerError) as invalid:
        CollectionReceipt(disposition, publication_id, artifact_count, True)
    assert invalid.value.code == "invalid_collector_receipt"


def registration(runner, *, authenticated: bool = False) -> CollectorRegistration:
    return CollectorRegistration(
        collector_id="synthetic-collector",
        plugin_id="synthetic-plugin",
        plugin_release="1.0.0",
        runner=runner,
        browser_realm="amazon-operations" if authenticated else None,
        authentication_required=authenticated,
    )


def auth_result(status: str, *, authenticated: bool, manual_action: str | None = None) -> AuthenticationResult:
    return AuthenticationResult(
        realm="amazon-operations",
        account_alias="default",
        status=status,
        authenticated=authenticated,
        manual_action=manual_action,
    )


def test_zero_collector_core_is_ready() -> None:
    manager = CollectionManager()

    assert manager.status() == {
        "ready": True,
        "status": "no_collectors",
        "registered": 0,
        "pending_manual_action": 0,
        "collectors": [],
        "durable_queue": {"status": "not_opened", "tasks": {}, "schedules": 0},
    }


def test_registration_and_request_contracts_are_closed_and_bounded() -> None:
    manager = CollectionManager()
    registered = manager.register(registration(lambda context: receipt()))

    assert registered["collector_id"] == "synthetic-collector"
    assert manager.status()["registered"] == 1
    with pytest.raises(CollectionManagerError, match="already registered"):
        manager.register(registration(lambda context: receipt()))
    with pytest.raises(CollectionManagerError, match="parameter type"):
        CollectionRequest("synthetic-collector", parameters={"target": {"nested": True}})
    with pytest.raises(CollectionManagerError, match="authenticated collectors"):
        CollectorRegistration(
            "bad-collector",
            "synthetic-plugin",
            "1.0.0",
            lambda context: receipt(),
            authentication_required=True,
        )
    timed = CollectorRegistration(
        "timed-collector",
        "synthetic-plugin",
        "1.0.0",
        lambda context: receipt(),
        execution_timeout_seconds=30,
    )
    assert timed.safe_data()["execution_timeout_seconds"] == 30
    with pytest.raises(CollectionManagerError, match="execution timeout"):
        CollectorRegistration(
            "too-slow",
            "synthetic-plugin",
            "1.0.0",
            lambda context: receipt(),
            execution_timeout_seconds=86_401,
        )


def test_nonbrowser_collection_returns_only_a_bounded_receipt() -> None:
    observed = {}

    def run(context):
        observed["context"] = context
        with pytest.raises(TypeError):
            context.parameters["date"] = "changed"
        return receipt()

    manager = CollectionManager()
    manager.register(registration(run))

    result = manager.run(
        CollectionRequest(
            "synthetic-collector",
            parameters={"date": "2026-08-14", "replace": False},
        )
    )

    assert result.ok is True
    assert result.status == "succeeded"
    assert result.safe_data()["receipt"] == {
        "disposition": "published",
        "publication_id": "synthetic-publication-1",
        "artifact_count": 1,
        "domain_complete": True,
    }
    assert observed["context"].session is None


def test_runner_failures_and_invalid_receipts_do_not_escape_details() -> None:
    def fail(context):
        raise RuntimeError("private-secret-marker")

    manager = CollectionManager()
    manager.register(registration(fail))
    failed = manager.run(CollectionRequest("synthetic-collector"))
    assert failed.status == "collector_failed"
    assert "private-secret-marker" not in repr(failed.safe_data())

    second = CollectionManager()
    second.register(registration(lambda context: {"status": "looks-valid"}))
    invalid = second.run(CollectionRequest("synthetic-collector"))
    assert invalid.status == "invalid_collector_receipt"


def test_collector_keyboard_interrupt_cancels_acquired_lease() -> None:
    browser = FakeBrowserManager()
    authentication = FakeAuthentication(auth_result("already_authenticated", authenticated=True))

    def interrupt(_context):
        raise KeyboardInterrupt("collector interrupted")

    manager = CollectionManager(browser, authentication)  # type: ignore[arg-type]
    manager.register(registration(interrupt, authenticated=True))
    with pytest.raises(KeyboardInterrupt):
        manager.run(CollectionRequest("synthetic-collector"))
    assert browser.leases[0].activated is True
    assert browser.leases[0].closed == "cancelled"


def test_cleanup_keyboard_interrupt_is_not_swallowed() -> None:
    browser = FakeBrowserManager()
    authentication = FakeAuthentication(auth_result("already_authenticated", authenticated=True))
    manager = CollectionManager(browser, authentication)  # type: ignore[arg-type]
    manager.register(registration(lambda _context: receipt(), authenticated=True))

    def interrupt_cleanup() -> TerminalLease:
        raise KeyboardInterrupt("cleanup interrupted")

    original_acquire = browser.acquire

    def acquire(request):
        lease = original_acquire(request)
        lease.release = interrupt_cleanup  # type: ignore[method-assign]
        return lease

    browser.acquire = acquire  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        manager.run(CollectionRequest("synthetic-collector"))


def test_runner_failure_with_cleanup_interrupt_returns_bounded_failure() -> None:
    browser = FakeBrowserManager()
    authentication = FakeAuthentication(auth_result("already_authenticated", authenticated=True))
    manager = CollectionManager(browser, authentication)  # type: ignore[arg-type]

    def fail_runner(_context):
        raise RuntimeError("collector failed")

    original_acquire = browser.acquire

    def acquire(request):
        lease = original_acquire(request)
        lease.cancel = lambda: (_ for _ in ()).throw(SystemExit("cleanup interrupted"))  # type: ignore[method-assign]
        return lease

    browser.acquire = acquire  # type: ignore[method-assign]
    manager.register(registration(fail_runner, authenticated=True))
    result = manager.run(CollectionRequest("synthetic-collector"))
    assert result.status == "browser_cleanup_failed"


def test_primary_interrupt_is_not_masked_by_cleanup_interrupt() -> None:
    browser = FakeBrowserManager()
    authentication = FakeAuthentication(auth_result("already_authenticated", authenticated=True))
    manager = CollectionManager(browser, authentication)  # type: ignore[arg-type]

    def interrupt_runner(_context):
        raise KeyboardInterrupt("primary interrupt")

    original_acquire = browser.acquire

    def acquire(request):
        lease = original_acquire(request)
        lease.cancel = lambda: (_ for _ in ()).throw(SystemExit("cleanup interrupt"))  # type: ignore[method-assign]
        return lease

    browser.acquire = acquire  # type: ignore[method-assign]
    manager.register(registration(interrupt_runner, authenticated=True))
    with pytest.raises(KeyboardInterrupt, match="primary interrupt"):
        manager.run(CollectionRequest("synthetic-collector"))


def test_acquire_cleanup_failure_preserves_browser_status() -> None:
    browser = FakeBrowserManager()
    authentication = FakeAuthentication(auth_result("already_authenticated", authenticated=True))

    def fail_acquire(_request):
        raise BrowserManagerError("browser_cleanup_failed", "quarantined browser")

    browser.acquire = fail_acquire  # type: ignore[method-assign]
    manager = CollectionManager(browser, authentication)  # type: ignore[arg-type]
    manager.register(registration(lambda _context: receipt(), authenticated=True))
    result = manager.run(CollectionRequest("synthetic-collector"))
    assert result.status == "browser_cleanup_failed"


def test_authenticated_collection_uses_a_headed_collection_lease() -> None:
    browser = FakeBrowserManager()
    authentication = FakeAuthentication(auth_result("already_authenticated", authenticated=True))
    observed = {}

    def run(context):
        observed["session"] = context.session
        return receipt()

    manager = CollectionManager(browser, authentication)
    manager.register(registration(run, authenticated=True))

    result = manager.run(CollectionRequest("synthetic-collector"))

    assert result.status == "succeeded"
    assert authentication.calls == ["authenticate:default"]
    assert browser.requests[0].purpose == BrowserPurpose.COLLECTION
    assert browser.requests[0].mode == BrowserMode.HEADED
    assert browser.leases[0].activated is True
    assert browser.leases[0].closed == "released"
    assert observed["session"] is browser.leases[0].session


def test_browser_activation_failure_cancels_the_acquired_lease() -> None:
    browser = FakeBrowserManager(activation_error=True)
    authentication = FakeAuthentication(auth_result("already_authenticated", authenticated=True))
    manager = CollectionManager(browser, authentication)
    manager.register(registration(lambda context: receipt(), authenticated=True))

    result = manager.run(CollectionRequest("synthetic-collector"))

    assert result.status == "browser_unavailable"
    assert browser.leases[0].closed == "cancelled"
    assert authentication.calls == []


def test_mfa_keeps_the_lease_until_explicit_resume_or_cancel() -> None:
    browser = FakeBrowserManager()
    authentication = FakeAuthentication(
        auth_result("mfa_required", authenticated=False, manual_action="complete_mfa"),
        auth_result("login_success", authenticated=True),
    )
    runs = []
    manager = CollectionManager(browser, authentication)
    manager.register(registration(lambda context: runs.append(context.run_id) or receipt(), authenticated=True))

    waiting = manager.run(CollectionRequest("synthetic-collector"))

    assert waiting.status == "mfa_required"
    assert waiting.manual_action == "complete_mfa"
    assert browser.leases[0].closed is None
    assert manager.status()["pending_manual_action"] == 1

    completed = manager.resume(waiting.run_id)
    assert completed.status == "succeeded"
    assert runs == [waiting.run_id]
    assert browser.leases[0].closed == "released"
    assert manager.status()["pending_manual_action"] == 0

    cancel_authentication = FakeAuthentication(
        auth_result("captcha_required", authenticated=False, manual_action="complete_captcha")
    )
    cancel_manager = CollectionManager(browser, cancel_authentication)
    cancel_manager.register(registration(lambda context: receipt(), authenticated=True))
    captcha = cancel_manager.run(CollectionRequest("synthetic-collector"))
    cancelled = cancel_manager.cancel(captcha.run_id)
    assert cancelled.status == "cancelled"
    assert browser.leases[-1].closed == "cancelled"


def test_durable_runner_interrupt_transitions_started_task_to_uncertain(tmp_path) -> None:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    store = CollectionTaskStore(root / "collection.sqlite3")
    now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    manager = CollectionManager(None, None, store, lambda: now)

    def interrupt(_context):
        raise KeyboardInterrupt("runner interrupted")

    manager.register(registration(interrupt))
    queued = manager.enqueue(CollectionRequest("synthetic-collector"))
    with pytest.raises(KeyboardInterrupt):
        manager.run_next("worker-one", 30)
    current = store.get(queued.task_id)
    assert current.state == TaskState.UNCERTAIN
    assert current.last_error_code == "collection_interrupted"


def test_interrupted_task_persistence_retries_terminal_interrupt(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    store = CollectionTaskStore(root / "collection.sqlite3")
    now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    manager = CollectionManager(None, None, store, lambda: now)
    manager.register(registration(lambda _context: (_ for _ in ()).throw(KeyboardInterrupt("runner"))))
    queued = manager.enqueue(CollectionRequest("synthetic-collector"))
    original_finish = store.finish
    interrupted = False

    class SyntheticPersistenceInterrupt(BaseException):
        pass

    def interrupt_once(*args, **kwargs):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise SyntheticPersistenceInterrupt("persist interrupted")
        return original_finish(*args, **kwargs)

    monkeypatch.setattr(store, "finish", interrupt_once)
    with pytest.raises(KeyboardInterrupt, match="runner"):
        manager.run_next("worker-one", 30)
    current = store.get(queued.task_id)
    assert current.state == TaskState.UNCERTAIN
    assert current.last_error_code == "collection_interrupted"


def test_initial_claim_read_interrupt_is_cleaned_and_rethrown(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    store = CollectionTaskStore(root / "collection.sqlite3")
    now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    manager = CollectionManager(None, None, store, lambda: now)
    manager.register(registration(lambda _context: receipt()))
    queued = manager.enqueue(CollectionRequest("synthetic-collector"))
    claimed = manager.claim_next("worker-one", 30)
    assert claimed is not None
    original_get = store.get
    interrupted = False

    def interrupt_once(task_id):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt("initial read")
        return original_get(task_id)

    monkeypatch.setattr(store, "get", interrupt_once)
    with pytest.raises(KeyboardInterrupt, match="initial read"):
        manager.run_claimed(queued.task_id, "worker-one")
    current = original_get(queued.task_id)
    assert current.state == TaskState.RETRY_WAIT
    assert current.last_error_code == "collection_interrupted"


def test_durable_resume_interrupt_leaves_no_running_task(tmp_path) -> None:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    store = CollectionTaskStore(root / "collection.sqlite3")
    now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    browser = FakeBrowserManager()
    authentication = FakeAuthentication(
        auth_result("mfa_required", authenticated=False, manual_action="complete_mfa")
    )
    manager = CollectionManager(browser, authentication, store, lambda: now)  # type: ignore[arg-type]
    manager.register(registration(lambda _context: receipt(), authenticated=True))
    queued = manager.enqueue(CollectionRequest("synthetic-collector"))
    waiting = manager.run_next("worker-one", 30)
    assert waiting is not None
    assert waiting.state == TaskState.WAITING_FOR_USER

    def interrupt_resume(_session, _account):
        raise KeyboardInterrupt("resume interrupted")

    authentication.resume = interrupt_resume  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        manager.resume_task(queued.task_id, 30)
    current = store.get(queued.task_id)
    assert current.state == TaskState.RETRY_WAIT
    assert current.last_error_code == "collection_interrupted"
    assert browser.leases[0].closed == "cancelled"


@pytest.mark.parametrize("method_name,post_mutation", [("request_resume", False), ("resume_waiting", True)])
def test_resume_state_transition_interrupt_cancels_lease_and_task(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    post_mutation: bool,
) -> None:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    store = CollectionTaskStore(root / "collection.sqlite3")
    now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    browser = FakeBrowserManager()
    authentication = FakeAuthentication(
        auth_result("mfa_required", authenticated=False, manual_action="complete_mfa")
    )
    manager = CollectionManager(browser, authentication, store, lambda: now)  # type: ignore[arg-type]
    manager.register(registration(lambda _context: receipt(), authenticated=True))
    queued = manager.enqueue(CollectionRequest("synthetic-collector"))
    waiting = manager.run_next("worker-one", 30)
    assert waiting is not None and waiting.state == TaskState.WAITING_FOR_USER
    original = getattr(store, method_name)

    def interrupt(*args, **kwargs):
        if post_mutation:
            original(*args, **kwargs)
        raise KeyboardInterrupt(f"{method_name} interrupted")

    monkeypatch.setattr(store, method_name, interrupt)
    with pytest.raises(KeyboardInterrupt):
        manager.resume_task(queued.task_id, 30)
    current = store.get(queued.task_id)
    assert current.state == TaskState.RETRY_WAIT
    assert current.last_error_code == "collection_interrupted"
    assert browser.leases[0].closed == "cancelled"
    assert manager.status()["pending_manual_action"] == 0


def test_durable_mfa_waits_and_resumes_the_same_task(tmp_path) -> None:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    store = CollectionTaskStore(root / "collection.sqlite3")
    now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    browser = FakeBrowserManager()
    authentication = FakeAuthentication(
        auth_result("mfa_required", authenticated=False, manual_action="complete_mfa"),
        auth_result("login_success", authenticated=True),
    )
    manager = CollectionManager(browser, authentication, store, lambda: now)
    manager.register(registration(lambda context: receipt(), authenticated=True))
    queued = manager.enqueue(CollectionRequest("synthetic-collector"))

    waiting = manager.run_next("worker-one", 30)
    assert waiting.task_id == queued.task_id
    assert waiting.state == TaskState.WAITING_FOR_USER
    assert browser.leases[0].closed is None

    completed = manager.resume_task(waiting.task_id, 30)
    assert completed.state == TaskState.SUCCEEDED
    assert browser.leases[0].closed == "released"


def test_durable_manual_task_honors_cancellation_from_another_process(tmp_path) -> None:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    store = CollectionTaskStore(root / "collection.sqlite3")
    now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    browser = FakeBrowserManager()
    authentication = FakeAuthentication(
        auth_result("mfa_required", authenticated=False, manual_action="complete_mfa"),
        auth_result("login_success", authenticated=True),
    )
    manager = CollectionManager(browser, authentication, store, lambda: now)
    manager.register(registration(lambda context: receipt(), authenticated=True))
    manager.enqueue(CollectionRequest("synthetic-collector"))
    waiting = manager.run_next("worker-one", 30)
    assert waiting.state == TaskState.WAITING_FOR_USER

    CollectionTaskStore(store.database).request_cancel(waiting.task_id, now)
    cancelled = manager.resume_task(waiting.task_id, 30)

    assert cancelled.state == TaskState.CANCELLED
    assert browser.leases[0].closed == "cancelled"


def test_durable_manual_cancellation_retains_guard_if_browser_cleanup_fails(tmp_path) -> None:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    store = CollectionTaskStore(root / "collection.sqlite3")
    now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    browser = FakeBrowserManager()
    authentication = FakeAuthentication(
        auth_result("mfa_required", authenticated=False, manual_action="complete_mfa")
    )
    manager = CollectionManager(browser, authentication, store, lambda: now)
    manager.register(registration(lambda context: receipt(), authenticated=True))
    manager.enqueue(CollectionRequest("synthetic-collector"))
    waiting = manager.run_next("worker-one", 30)
    assert waiting is not None

    def fail_cleanup():
        raise RuntimeError("private cleanup detail")

    browser.leases[0].cancel = fail_cleanup
    cancelled = manager.cancel_task(waiting.task_id)

    assert cancelled.state == TaskState.CANCELLED
    assert cancelled.last_error_code == "cancelled"
    assert manager.status()["pending_manual_action"] == 1
    assert browser.leases[0].closed is None
