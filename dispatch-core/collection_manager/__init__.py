"""Bounded collector registration and synchronous Core orchestration."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
import hashlib
import math
import re
import secrets
from typing import TYPE_CHECKING, Any, Callable, Mapping

from browser_manager import (
    BrowserLeaseRequest,
    BrowserManager,
    BrowserManagerError,
    BrowserMode,
    BrowserPurpose,
    LeaseState,
    ManagedBrowserSession,
    ManagedLease,
)
from paths import DispatchPaths

if TYPE_CHECKING:
    from authentication import AuthenticationManager, AuthenticationResult

from .queue import (
    _confirmed_publication_absence,
    CollectionStoreError,
    CollectionTaskStore,
    ScheduleRecord,
    TaskRecord,
    TaskState,
    retry_delay,
    utc_now,
)


_SLUG = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_RELEASE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_RUN_ID = re.compile(r"^[0-9a-f]{32}$")
_MAX_PARAMETERS = 16
_MAX_PARAMETER_TEXT = 256
_MAX_EXECUTION_TIMEOUT_SECONDS = 86_400.0
MAX_EXECUTION_TIMEOUT_SECONDS = _MAX_EXECUTION_TIMEOUT_SECONDS


class CollectionManagerError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CollectionDisposition(StrEnum):
    PUBLISHED = "published"
    SKIPPED_EXISTING = "skipped_existing"
    NO_DATA = "no_data"


class PublicationVerification(StrEnum):
    ABSENT = "absent"


def _slug(value: str, label: str) -> str:
    if not isinstance(value, str) or len(value) > 63 or not _SLUG.fullmatch(value):
        raise CollectionManagerError("invalid_collection_request", f"{label} must be a lowercase Dispatch slug")
    return value


def _parameters(values: Mapping[str, object]) -> Mapping[str, object]:
    if not isinstance(values, Mapping) or len(values) > _MAX_PARAMETERS:
        raise CollectionManagerError("invalid_collection_request", "collection parameters are invalid")
    normalized: dict[str, object] = {}
    for key, value in values.items():
        _slug(key, "parameter name")
        if isinstance(value, str):
            if not value or len(value) > _MAX_PARAMETER_TEXT or "\x00" in value:
                raise CollectionManagerError("invalid_collection_request", "collection parameter text is invalid")
        elif type(value) is int:
            if abs(value) > 1_000_000_000:
                raise CollectionManagerError("invalid_collection_request", "collection parameter number is invalid")
        elif not isinstance(value, bool) and value is not None:
            raise CollectionManagerError("invalid_collection_request", "collection parameter type is invalid")
        normalized[key] = value
    return MappingProxyType(normalized)


@dataclass(frozen=True)
class CollectionRequest:
    collector_id: str
    account_alias: str = "default"
    parameters: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _slug(self.collector_id, "collector_id")
        _slug(self.account_alias, "account_alias")
        object.__setattr__(self, "parameters", _parameters(self.parameters))


@dataclass(frozen=True)
class CollectionReceipt:
    disposition: CollectionDisposition
    publication_id: str | None
    artifact_count: int
    domain_complete: bool

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, CollectionDisposition):
            raise CollectionManagerError("invalid_collector_receipt", "collector disposition is invalid")
        if self.publication_id is not None and (
            not isinstance(self.publication_id, str) or not _RELEASE.fullmatch(self.publication_id)
        ):
            raise CollectionManagerError("invalid_collector_receipt", "publication identity is invalid")
        if type(self.artifact_count) is not int or not 0 <= self.artifact_count <= 1000:
            raise CollectionManagerError("invalid_collector_receipt", "artifact count is invalid")
        if not isinstance(self.domain_complete, bool):
            raise CollectionManagerError("invalid_collector_receipt", "domain completeness is invalid")
        if self.disposition in {CollectionDisposition.PUBLISHED, CollectionDisposition.SKIPPED_EXISTING} and self.publication_id is None:
            raise CollectionManagerError("invalid_collector_receipt", "published or retained work requires an identity")
        if self.disposition == CollectionDisposition.PUBLISHED and self.artifact_count == 0:
            raise CollectionManagerError("invalid_collector_receipt", "published work requires an artifact")
        if self.disposition == CollectionDisposition.SKIPPED_EXISTING and self.artifact_count != 0:
            raise CollectionManagerError("invalid_collector_receipt", "retained work cannot report new artifacts")
        if self.disposition == CollectionDisposition.NO_DATA and (
            self.publication_id is not None or self.artifact_count != 0
        ):
            raise CollectionManagerError("invalid_collector_receipt", "no-data work cannot report publication artifacts")

    def safe_data(self) -> dict[str, object]:
        return {
            "disposition": self.disposition.value,
            "publication_id": self.publication_id,
            "artifact_count": self.artifact_count,
            "domain_complete": self.domain_complete,
        }


@dataclass(frozen=True)
class CollectionContext:
    run_id: str
    collector_id: str
    plugin_id: str
    parameters: Mapping[str, object]
    session: ManagedBrowserSession | None = field(repr=False, compare=False)


CollectorRunner = Callable[[CollectionContext], CollectionReceipt]


@dataclass(frozen=True)
class CollectorRegistration:
    collector_id: str
    plugin_id: str
    plugin_release: str
    runner: CollectorRunner = field(repr=False, compare=False)
    browser_realm: str | None = None
    authentication_required: bool = False
    publication_verifier: Callable[[CollectionRequest, Mapping[str, object] | None], PublicationVerification] | None = field(
        default=None, repr=False, compare=False
    )
    execution_timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        _slug(self.collector_id, "collector_id")
        _slug(self.plugin_id, "plugin_id")
        if not isinstance(self.plugin_release, str) or not _RELEASE.fullmatch(self.plugin_release):
            raise CollectionManagerError("invalid_collector_registration", "plugin release is invalid")
        if not callable(self.runner):
            raise CollectionManagerError("invalid_collector_registration", "collector runner is not callable")
        if self.browser_realm is not None:
            _slug(self.browser_realm, "browser_realm")
        if not isinstance(self.authentication_required, bool):
            raise CollectionManagerError("invalid_collector_registration", "authentication requirement is invalid")
        if self.authentication_required and self.browser_realm is None:
            raise CollectionManagerError(
                "invalid_collector_registration",
                "authenticated collectors require a browser realm",
            )
        if self.execution_timeout_seconds is not None:
            try:
                timeout_valid = (
                    type(self.execution_timeout_seconds) in {int, float}
                    and math.isfinite(float(self.execution_timeout_seconds))
                    and 0.1 <= float(self.execution_timeout_seconds) <= _MAX_EXECUTION_TIMEOUT_SECONDS
                )
            except (OverflowError, ValueError):
                timeout_valid = False
            if not timeout_valid:
                raise CollectionManagerError(
                    "invalid_collector_registration",
                    "collector execution timeout is invalid",
                )
        if self.publication_verifier is not None and not callable(self.publication_verifier):
            raise CollectionManagerError("invalid_collector_registration", "publication verifier must be callable")

    def safe_data(self) -> dict[str, object]:
        data = {
            "collector_id": self.collector_id,
            "plugin_id": self.plugin_id,
            "plugin_release": self.plugin_release,
            "browser_realm": self.browser_realm,
            "authentication_required": self.authentication_required,
        }
        if self.execution_timeout_seconds is not None:
            data["execution_timeout_seconds"] = self.execution_timeout_seconds
        return data


@dataclass(frozen=True)
class CollectionResult:
    run_id: str
    collector_id: str
    plugin_id: str
    status: str
    ok: bool
    manual_action: str | None = None
    receipt: CollectionReceipt | None = None

    def safe_data(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "collector_id": self.collector_id,
            "plugin_id": self.plugin_id,
            "status": self.status,
            "ok": self.ok,
            "manual_action": self.manual_action,
            "receipt": None if self.receipt is None else self.receipt.safe_data(),
        }


@dataclass
class _PendingCollection:
    registration: CollectorRegistration
    request: CollectionRequest
    lease: ManagedLease
    before_execute: Callable[[], None] | None = None


class CollectionManager:
    """Collector registry, bounded executor, and optional durable queue."""

    def __init__(
        self,
        browser_manager: BrowserManager | None = None,
        authentication: AuthenticationManager | None = None,
        store: CollectionTaskStore | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._browser = browser_manager
        self._authentication = authentication
        self._store = store
        self._clock = clock
        self._collectors: dict[str, CollectorRegistration] = {}
        self._pending: dict[str, _PendingCollection] = {}

    @classmethod
    def production(
        cls,
        paths: DispatchPaths,
        *,
        browser_manager: BrowserManager | None = None,
        authentication: AuthenticationManager | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> "CollectionManager":
        return cls(
            browser_manager,
            authentication,
            CollectionTaskStore.from_paths(paths),
            clock,
        )

    def register(self, registration: CollectorRegistration) -> dict[str, object]:
        if not isinstance(registration, CollectorRegistration):
            raise CollectionManagerError("invalid_collector_registration", "collector registration is invalid")
        if registration.collector_id in self._collectors:
            raise CollectionManagerError("collector_already_registered", "collector identity is already registered")
        self._collectors[registration.collector_id] = registration
        return registration.safe_data()

    def status(self) -> dict[str, object]:
        registrations = [self._collectors[key].safe_data() for key in sorted(self._collectors)]
        if self._store is None:
            queue: dict[str, object] = {"status": "not_opened", "tasks": {}, "schedules": 0}
        else:
            queue = {
                "status": "ready",
                "tasks": self._store.counts(),
                "schedules": len(self._store.schedules()),
                "workers": self._store.process_worker_count(),
            }
        return {
            "ready": True,
            "status": "ready" if registrations else "no_collectors",
            "registered": len(registrations),
            "pending_manual_action": len(self._pending),
            "collectors": registrations,
            "durable_queue": queue,
        }

    def _resolve_auth_request(
        self,
        request: CollectionRequest,
        registration: CollectorRegistration,
    ) -> CollectionRequest:
        if not registration.authentication_required or self._authentication is None or registration.browser_realm is None:
            return request
        if not hasattr(self._authentication, "profile_for_plugin"):
            return request
        try:
            if request.account_alias != "default":
                # Compatibility aliases are explicit durable task identity. Check
                # enrollment now, then preserve the alias through queue execution.
                self._authentication.credentials(registration.browser_realm, request.account_alias)
                return request
            profile = self._authentication.profile_for_plugin(
                registration.plugin_id,
                registration.browser_realm,
            )
            alias = self._authentication.account_alias_for_profile(profile, registration.browser_realm)
        except Exception as exc:
            from authentication import AuthenticationError

            if isinstance(exc, AuthenticationError):
                raise CollectionManagerError(
                    "authentication_profile_required",
                    "an enrolled authentication profile is required before collection",
                ) from exc
            raise
        return CollectionRequest(
            collector_id=request.collector_id,
            account_alias=alias,
            parameters=request.parameters,
        )

    def enqueue(
        self,
        request: CollectionRequest,
        *,
        max_attempts: int = 3,
        not_before: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> TaskRecord:
        registration = self._registration(request)
        request = self._resolve_auth_request(request, registration)
        return self._require_store().enqueue(
            collector_id=request.collector_id,
            account_alias=request.account_alias,
            parameters=request.parameters,
            max_attempts=max_attempts,
            at=self._clock(),
            not_before=not_before,
            idempotency_key=idempotency_key,
        )

    def task(self, task_id: str) -> TaskRecord:
        return self._require_store().get(task_id)

    def tasks(self, limit: int = 50) -> list[TaskRecord]:
        return self._require_store().recent(limit)

    def claim_next(self, worker_id: str = "collection-worker", lease_seconds: int = 900) -> TaskRecord | None:
        return self._require_store().claim(
            worker_id,
            self._clock(),
            lease_seconds,
            tuple(sorted(self._collectors)),
        )

    def run_claimed(self, task_id: str, worker_id: str) -> TaskRecord:
        store = self._require_store()
        try:
            claimed = store.get(task_id)
        except BaseException:
            try:
                self._persist_interrupted_task(task_id, worker_id)
            except BaseException:
                pass
            raise
        if claimed.state != TaskState.RUNNING or claimed.worker_id != worker_id or claimed.execution_started:
            raise CollectionManagerError("collection_state_conflict", "worker does not own an unstarted collection task")
        registration = self._collectors.get(claimed.collector_id)
        if registration is None:
            return self._persist_failure(claimed, worker_id, "collector_not_registered")
        request = CollectionRequest(
            collector_id=claimed.collector_id,
            account_alias=claimed.account_alias,
            parameters=claimed.parameters,
        )
        def mark_started() -> None:
            store.mark_execution_started(claimed.task_id, worker_id, self._clock())

        try:
            result = self._run_request(
                request,
                claimed.task_id,
                mark_started,
                resolve_profile=False,
            )
            return self._persist_result(claimed.task_id, worker_id, result)
        except BaseException as primary:
            pending = self._pending.get(claimed.task_id)
            try:
                self._persist_interrupted_task(claimed.task_id, worker_id)
            except BaseException:
                raise primary
            if pending is not None:
                self._pending.pop(claimed.task_id, None)
                self._finish_lease(pending.lease, success=False, primary=primary)
            raise

    def run_next(self, worker_id: str = "collection-worker", lease_seconds: int = 900) -> TaskRecord | None:
        claimed = self.claim_next(worker_id, lease_seconds)
        if claimed is None:
            return None
        return self.run_claimed(claimed.task_id, worker_id)

    def request_resume_task(self, task_id: str) -> TaskRecord:
        return self._require_store().request_resume(task_id, self._clock())

    def resume_task(self, task_id: str, lease_seconds: int = 900) -> TaskRecord:
        store = self._require_store()
        try:
            current = store.get(task_id)
        except BaseException as primary:
            pending = self._pending.get(task_id)
            try:
                self._persist_interrupted_owner(task_id)
            except BaseException:
                raise primary
            if pending is not None:
                self._pending.pop(task_id, None)
                self._finish_lease(pending.lease, success=False, primary=primary)
            raise
        if current.state != TaskState.WAITING_FOR_USER or current.worker_id is None or task_id not in self._pending:
            raise CollectionManagerError("collection_not_pending", "collection is not resumable in this worker")
        worker_id = current.worker_id
        assert worker_id is not None
        pending_guard = self._pending[task_id]
        if current.cancel_requested:
            return self._finish_pending_cancellation(task_id, worker_id)
        try:
            if not current.resume_requested:
                current = store.request_resume(task_id, self._clock())
            store.resume_waiting(task_id, worker_id, self._clock(), lease_seconds)
            result = self.resume(task_id)
            return self._persist_result(task_id, worker_id, result)
        except BaseException as primary:
            pending = self._pending.get(task_id, pending_guard)
            try:
                self._persist_interrupted_task(task_id, worker_id)
            except BaseException:
                raise primary
            self._pending.pop(task_id, None)
            self._finish_lease(pending.lease, success=False, primary=primary)
            raise

    def cancel_task(self, task_id: str) -> TaskRecord:
        store = self._require_store()
        try:
            current = store.request_cancel(task_id, self._clock())
        except BaseException as primary:
            pending = self._pending.get(task_id)
            try:
                self._persist_interrupted_owner(task_id)
            except BaseException:
                raise primary
            if pending is not None:
                self._pending.pop(task_id, None)
                self._finish_lease(pending.lease, success=False, primary=primary)
            raise
        if current.state == TaskState.WAITING_FOR_USER and task_id in self._pending:
            return self._finish_pending_cancellation(task_id, current.worker_id)
        return current

    def heartbeat_task(self, task_id: str, worker_id: str, lease_seconds: int = 900) -> TaskRecord:
        return self._require_store().heartbeat(task_id, worker_id, self._clock(), lease_seconds)

    def retry_task(self, task_id: str) -> TaskRecord:
        store = self._require_store()
        current = store.get(task_id)
        publication_id = None if current.receipt is None else current.receipt.get("publication_id")
        if current.state == TaskState.UNCERTAIN or publication_id is not None:
            registration = self._collectors.get(current.collector_id)
            if registration is None or registration.publication_verifier is None:
                raise CollectionManagerError(
                    "publication_verification_required",
                    "collector publication verification is required before retry",
                )
            request = CollectionRequest(current.collector_id, current.account_alias, current.parameters)
            try:
                result = registration.publication_verifier(request, current.receipt)
            except Exception as exc:
                raise CollectionManagerError(
                    "publication_verification_failed",
                    "collector could not verify publication absence",
                ) from exc
            if result is not PublicationVerification.ABSENT:
                raise CollectionManagerError(
                    "publication_verification_failed",
                    "collector did not verify publication absence",
                )
            at = self._clock()
            verification = _confirmed_publication_absence(current, at)
            return store.retry(task_id, at, verification=verification)
        return store.retry(task_id, self._clock())

    def reconcile_tasks(self) -> list[TaskRecord]:
        store = self._require_store()
        now = self._clock()
        for task_id in list(self._pending):
            current = store.get(task_id)
            if current.lease_expires_at is not None and current.lease_expires_at <= now:
                result = self.cancel(task_id)
                if result.status == "browser_cleanup_failed":
                    store.finish(
                        task_id,
                        current.worker_id,
                        TaskState.FAILED,
                        now,
                        error_code="browser_cleanup_failed",
                    )
        return store.reconcile(now)

    def create_schedule(
        self,
        request: CollectionRequest,
        *,
        schedule_key: str,
        interval_seconds: int,
        next_run_at: datetime,
        max_attempts: int = 3,
    ) -> ScheduleRecord:
        registration = self._registration(request)
        request = self._resolve_auth_request(request, registration)
        _slug(schedule_key, "schedule_key")
        schedule_id = hashlib.sha256(
            f"dispatch-collection-schedule-v1\0{request.collector_id}\0{request.account_alias}\0{schedule_key}".encode()
        ).hexdigest()[:32]
        return self._require_store().create_schedule(
            schedule_id=schedule_id,
            collector_id=request.collector_id,
            account_alias=request.account_alias,
            parameters=request.parameters,
            interval_seconds=interval_seconds,
            next_run_at=next_run_at,
            max_attempts=max_attempts,
            at=self._clock(),
        )

    def schedules(self) -> list[ScheduleRecord]:
        return self._require_store().schedules()

    def set_schedule_enabled(self, schedule_id: str, enabled: bool) -> ScheduleRecord:
        return self._require_store().set_schedule_enabled(schedule_id, enabled, self._clock())

    def enqueue_due(self) -> list[TaskRecord]:
        return self._require_store().enqueue_due(self._clock())

    def run(self, request: CollectionRequest) -> CollectionResult:
        return self._run_request(request, secrets.token_hex(16), None)

    def _run_request(
        self,
        request: CollectionRequest,
        run_id: str,
        before_execute: Callable[[], None] | None,
        *,
        resolve_profile: bool = True,
    ) -> CollectionResult:
        if not isinstance(request, CollectionRequest):
            raise CollectionManagerError("invalid_collection_request", "collection request is invalid")
        registration = self._registration(request)
        if resolve_profile:
            request = self._resolve_auth_request(request, registration)
        if registration.browser_realm is None:
            return self._execute(run_id, registration, request, None, None, before_execute)
        if self._browser is None or (registration.authentication_required and self._authentication is None):
            return self._result(run_id, registration, "collection_dependencies_unavailable")

        lease: ManagedLease | None = None
        try:
            lease = self._browser.acquire(
                BrowserLeaseRequest(
                    plugin_id=registration.plugin_id,
                    plugin_release=registration.plugin_release,
                    realm=registration.browser_realm,
                    purpose=BrowserPurpose.COLLECTION,
                    account_alias=request.account_alias,
                    mode=BrowserMode.HEADED if registration.authentication_required else BrowserMode.HEADLESS,
                )
            )
            lease.activate()
        except BaseException as exc:
            cleanup_ok = self._finish_lease(lease, success=False, primary=exc)
            if not isinstance(exc, Exception):
                raise
            cleanup_failed = (
                isinstance(exc, BrowserManagerError) and exc.code == "browser_cleanup_failed"
            ) or not cleanup_ok
            return self._result(
                run_id,
                registration,
                "browser_cleanup_failed" if cleanup_failed else "browser_unavailable",
            )

        if not registration.authentication_required:
            return self._execute(run_id, registration, request, lease, lease.session, before_execute)
        assert self._authentication is not None
        try:
            authentication = self._authentication.authenticate(lease.session, request.account_alias)
        except BaseException as exc:
            cleanup_ok = self._finish_lease(lease, success=False, primary=exc)
            if not isinstance(exc, Exception):
                raise
            return self._result(
                run_id,
                registration,
                "authentication_unavailable" if cleanup_ok else "browser_cleanup_failed",
            )
        return self._after_authentication(
            run_id,
            registration,
            request,
            lease,
            authentication,
            before_execute,
        )

    def resume(self, run_id: str) -> CollectionResult:
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise CollectionManagerError("invalid_collection_request", "run identity is invalid")
        pending = self._pending.get(run_id)
        if pending is None:
            raise CollectionManagerError("collection_not_pending", "collection is not waiting for manual action")
        assert self._authentication is not None
        try:
            authentication = self._authentication.resume(
                pending.lease.session,
                pending.request.account_alias,
            )
        except BaseException as exc:
            self._pending.pop(run_id, None)
            cleanup_ok = self._finish_lease(pending.lease, success=False, primary=exc)
            if not isinstance(exc, Exception):
                raise
            return self._result(
                run_id,
                pending.registration,
                "authentication_unavailable" if cleanup_ok else "browser_cleanup_failed",
            )
        return self._after_authentication(
            run_id,
            pending.registration,
            pending.request,
            pending.lease,
            authentication,
            pending.before_execute,
        )

    def cancel(self, run_id: str) -> CollectionResult:
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise CollectionManagerError("invalid_collection_request", "run identity is invalid")
        pending = self._pending.pop(run_id, None)
        if pending is None:
            raise CollectionManagerError("collection_not_pending", "collection is not waiting for manual action")
        cleanup_ok = self._finish_lease(pending.lease, success=False)
        return self._result(
            run_id,
            pending.registration,
            "cancelled" if cleanup_ok else "browser_cleanup_failed",
        )

    def _after_authentication(
        self,
        run_id: str,
        registration: CollectorRegistration,
        request: CollectionRequest,
        lease: ManagedLease,
        authentication: AuthenticationResult,
        before_execute: Callable[[], None] | None,
    ) -> CollectionResult:
        if authentication.authenticated:
            self._pending.pop(run_id, None)
            return self._execute(run_id, registration, request, lease, lease.session, before_execute)
        if authentication.status in {"mfa_required", "captcha_required"}:
            self._pending[run_id] = _PendingCollection(registration, request, lease, before_execute)
            return self._result(
                run_id,
                registration,
                authentication.status,
                manual_action=authentication.manual_action,
            )
        self._pending.pop(run_id, None)
        cleanup_ok = self._finish_lease(lease, success=False)
        return self._result(
            run_id,
            registration,
            authentication.status if cleanup_ok else "browser_cleanup_failed",
        )

    def _execute(
        self,
        run_id: str,
        registration: CollectorRegistration,
        request: CollectionRequest,
        lease: ManagedLease | None,
        session: ManagedBrowserSession | None,
        before_execute: Callable[[], None] | None,
    ) -> CollectionResult:
        context = CollectionContext(
            run_id=run_id,
            collector_id=registration.collector_id,
            plugin_id=registration.plugin_id,
            parameters=request.parameters,
            session=session,
        )
        if before_execute is not None:
            try:
                before_execute()
            except BaseException as exc:
                cleanup_ok = self._finish_lease(lease, success=False, primary=exc)
                if not isinstance(exc, Exception):
                    raise
                return self._result(
                    run_id,
                    registration,
                    "task_state_unavailable" if cleanup_ok else "browser_cleanup_failed",
                )
        try:
            receipt = registration.runner(context)
        except BaseException as exc:
            cleanup_ok = self._finish_lease(lease, success=False, primary=exc)
            if not isinstance(exc, Exception):
                raise
            return self._result(
                run_id,
                registration,
                "collector_failed" if cleanup_ok else "browser_cleanup_failed",
            )
        if not isinstance(receipt, CollectionReceipt):
            cleanup_ok = self._finish_lease(lease, success=False)
            return self._result(
                run_id,
                registration,
                "invalid_collector_receipt" if cleanup_ok else "browser_cleanup_failed",
            )
        cleanup_ok = self._finish_lease(lease, success=True)
        if not cleanup_ok:
            return self._result(run_id, registration, "browser_cleanup_failed", receipt=receipt)
        return self._result(run_id, registration, "succeeded", ok=True, receipt=receipt)

    def _finish_pending_cancellation(self, task_id: str, worker_id: str) -> TaskRecord:
        guard = self._pending.get(task_id)
        store = self._require_store()
        last_error: BaseException | None = None
        terminal: TaskRecord | None = None
        for _attempt in range(3):
            try:
                current = store.get(task_id)
                if current.state == TaskState.CANCELLED:
                    terminal = current
                else:
                    terminal = store.finish(
                        task_id,
                        worker_id,
                        TaskState.CANCELLED,
                        self._clock(),
                        error_code="cancelled",
                    )
                break
            except BaseException as exc:
                last_error = exc
        if terminal is None:
            assert last_error is not None
            raise last_error
        cleanup_ok = True if guard is None else self._finish_lease(guard.lease, success=False)
        if cleanup_ok:
            self._pending.pop(task_id, None)
        return terminal

    def _persist_interrupted_task(self, task_id: str, worker_id: str) -> TaskRecord:
        store = self._require_store()
        last_error: BaseException | None = None
        for _attempt in range(3):
            try:
                current = store.get(task_id)
                if current.state not in {TaskState.RUNNING, TaskState.WAITING_FOR_USER} or current.worker_id != worker_id:
                    return current
                if current.execution_started:
                    return store.finish(
                        task_id,
                        worker_id,
                        TaskState.UNCERTAIN,
                        self._clock(),
                        error_code="collection_interrupted",
                    )
                return self._persist_failure(current, worker_id, "collection_interrupted")
            except BaseException as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    def _persist_interrupted_owner(self, task_id: str) -> TaskRecord:
        store = self._require_store()
        last_error: BaseException | None = None
        for _attempt in range(3):
            try:
                current = store.get(task_id)
                if current.worker_id is None:
                    return current
                return self._persist_interrupted_task(task_id, current.worker_id)
            except BaseException as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    def _persist_result(
        self,
        task_id: str,
        worker_id: str,
        result: CollectionResult,
    ) -> TaskRecord:
        store = self._require_store()
        current = store.get(task_id)
        if result.status in {"mfa_required", "captcha_required"}:
            if current.cancel_requested:
                return self._finish_pending_cancellation(task_id, worker_id)
            try:
                return store.wait_for_user(task_id, worker_id, result.status, self._clock())
            except CollectionStoreError as exc:
                if exc.code != "collection_state_conflict":
                    if task_id in self._pending:
                        self.cancel(task_id)
                    raise
                current = store.get(task_id)
                if not current.cancel_requested:
                    if task_id in self._pending:
                        self.cancel(task_id)
                    raise
                return self._finish_pending_cancellation(task_id, worker_id)
        if result.ok and result.receipt is not None:
            return store.finish(
                task_id,
                worker_id,
                TaskState.SUCCEEDED,
                self._clock(),
                receipt=result.receipt.safe_data(),
            )
        current = store.get(task_id)
        if current.execution_started:
            if result.receipt is not None:
                return store.finish(
                    task_id,
                    worker_id,
                    TaskState.FAILED,
                    self._clock(),
                    error_code=result.status,
                    receipt=result.receipt.safe_data(),
                )
            return store.finish(
                task_id,
                worker_id,
                TaskState.UNCERTAIN,
                self._clock(),
                error_code=result.status,
            )
        if current.cancel_requested:
            return store.finish(
                task_id,
                worker_id,
                TaskState.CANCELLED,
                self._clock(),
                error_code="cancelled",
            )
        return self._persist_failure(current, worker_id, result.status)

    def _persist_failure(self, task: TaskRecord, worker_id: str, error_code: str) -> TaskRecord:
        transient = {
            "authentication_unavailable",
            "browser_cleanup_failed",
            "browser_unavailable",
            "collection_dependencies_unavailable",
            "collection_interrupted",
            "collector_not_registered",
            "task_state_unavailable",
        }
        store = self._require_store()
        task = store.get(task.task_id)
        now = self._clock()
        if task.cancel_requested:
            return store.finish(
                task.task_id,
                worker_id,
                TaskState.CANCELLED,
                now,
                error_code="cancelled",
            )
        if error_code in transient and task.attempt_count < task.max_attempts:
            return store.finish(
                task.task_id,
                worker_id,
                TaskState.RETRY_WAIT,
                now,
                error_code=error_code,
                not_before=now + retry_delay(task.attempt_count),
            )
        return store.finish(
            task.task_id,
            worker_id,
            TaskState.FAILED,
            now,
            error_code=error_code,
        )

    def _registration(self, request: CollectionRequest) -> CollectorRegistration:
        if not isinstance(request, CollectionRequest):
            raise CollectionManagerError("invalid_collection_request", "collection request is invalid")
        registration = self._collectors.get(request.collector_id)
        if registration is None:
            raise CollectionManagerError("collector_not_registered", "collector is not registered")
        return registration

    def _require_store(self) -> CollectionTaskStore:
        if self._store is None:
            raise CollectionManagerError("collection_store_unavailable", "durable collection storage is not open")
        return self._store

    @staticmethod
    def _finish_lease(
        lease: ManagedLease | None,
        *,
        success: bool,
        primary: BaseException | None = None,
    ) -> bool:
        if lease is None:
            return True
        try:
            terminal = lease.release() if success else lease.cancel()
        except BaseException as cleanup_error:
            if isinstance(cleanup_error, Exception):
                return False
            if primary is None:
                raise
            return False
        expected = LeaseState.CLOSED if success else LeaseState.CANCELLED
        return terminal.state == expected

    @staticmethod
    def _result(
        run_id: str,
        registration: CollectorRegistration,
        status: str,
        *,
        ok: bool = False,
        manual_action: str | None = None,
        receipt: CollectionReceipt | None = None,
    ) -> CollectionResult:
        return CollectionResult(
            run_id=run_id,
            collector_id=registration.collector_id,
            plugin_id=registration.plugin_id,
            status=status,
            ok=ok,
            manual_action=manual_action,
            receipt=receipt,
        )


from .supervisor import (
    CollectionService,
    CollectionWorkerSupervisor,
    ProductionManagerFactory,
    ServiceTick,
    WorkerOutcome,
    WorkerPolicy,
)


__all__ = [
    "CollectionService",
    "CollectionWorkerSupervisor",
    "CollectionContext",
    "CollectionDisposition",
    "CollectionManager",
    "CollectionManagerError",
    "CollectionReceipt",
    "CollectionRequest",
    "CollectionResult",
    "CollectionStoreError",
    "CollectionTaskStore",
    "CollectorRegistration",
    "CollectorRunner",
    "PublicationVerification",
    "ProductionManagerFactory",
    "ScheduleRecord",
    "TaskRecord",
    "TaskState",
    "ServiceTick",
    "WorkerOutcome",
    "WorkerPolicy",
    "MAX_EXECUTION_TIMEOUT_SECONDS",
]
