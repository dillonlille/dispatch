"""Core CollectorRunner for complete Paycom timecards."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any, Callable, Iterator

from collection_manager import CollectionContext, CollectionDisposition, CollectionReceipt

from dispatch_paycom.roster.storage import RosterStorageError, RosterStore
from .artifacts import TimecardArtifact, TimecardArtifactWriter, discard_artifact_run
from .browser import TimecardBrowserError, TimecardCapture, capture_timecard
from .period import Period, period_from_end
from .storage import TimecardStorageError, TimecardStore

PLUGIN_ID = "paycom"
COLLECTOR_ID = "paycom-timecards"
PLUGIN_RELEASE = "0.1.0"
BROWSER_REALM = "paycom-client"
LANDING_URL = "https://www.paycomonline.net/v4/cl/web.php/client-landing/arc"


class TimecardCollectorError(RuntimeError):
    pass


def _paths() -> tuple[Path, Path, Path]:
    import importlib

    from dispatch_paycom.paths import PaycomPaths

    core_paths = importlib.import_module("paths")
    resolved = core_paths.DispatchPaths.from_environment()
    data = PaycomPaths.from_dispatch(resolved)
    return resolved.owner_root("cache", PLUGIN_ID) / "timecard-staging", data.timecards, data.roster


def _page(context: Any) -> Any:
    session = getattr(context, "session", None)
    if session is None or getattr(session, "realm", None) != BROWSER_REALM or getattr(session, "landing_url", None) != LANDING_URL or getattr(session, "page", None) is None:
        raise TimecardCollectorError("managed_browser_session_required")
    return session.page


@contextmanager
def _lock(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    path = root / ".collector.lock"
    with path.open("a+b") as handle:
        os.chmod(path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def collect_timecards(
    context: CollectionContext,
    *,
    root: Path | None = None,
    db_path: Path | None = None,
    roster_path: Path | None = None,
    capture: Callable[..., TimecardCapture] = capture_timecard,
) -> CollectionReceipt:
    page = _page(context)
    parameters = getattr(context, "parameters", {})
    period_end = parameters.get("period_end", parameters.get("target")) if hasattr(parameters, "get") else None
    if not isinstance(period_end, str):
        raise TimecardCollectorError("period_end_required")
    try:
        period: Period = period_from_end(period_end)
    except ValueError as exc:
        raise TimecardCollectorError("invalid_period") from exc
    configured_root, configured_db, configured_roster = _paths() if root is None or db_path is None or roster_path is None else (Path(root), Path(db_path), Path(roster_path))
    artifact_root = Path(root) if root is not None else configured_root
    database = Path(db_path) if db_path is not None else configured_db
    roster_database = Path(roster_path) if roster_path is not None else configured_roster
    if not roster_database.is_file() or roster_database.is_symlink():
        raise TimecardCollectorError("roster_missing")
    collected_at = datetime.now(timezone.utc).isoformat()
    writer: TimecardArtifactWriter | None = None
    artifact: TimecardArtifact | None = None
    with _lock(artifact_root):
        roster_store = RosterStore(roster_database)
        try:
            roster_revision, roster_sha, employees = roster_store.latest_active_employees()
        except (RosterStorageError, OSError) as exc:
            roster_store.close()
            raise TimecardCollectorError(str(exc)) from exc
        roster_store.close()
        expected_codes = [code for code, _name in employees]
        writer = TimecardArtifactWriter(artifact_root, period, expected_codes)
        captures: list[dict[str, Any]] = []
        try:
            for index, (code, name) in enumerate(employees):
                capture_result = capture(page, employee_code=code, period=period, variant=(index % 2) + 1)
                if not isinstance(capture_result, TimecardCapture) or capture_result.employee_code != code or capture_result.period_key != period.key:
                    raise TimecardCollectorError("employee_membership_mismatch")
                writer.add(capture_result)
                captures.append({"employeeCode": code, "employeeName": name, "record": capture_result.record})
            if {item["employeeCode"] for item in captures} != set(expected_codes):
                raise TimecardCollectorError("employee_membership_mismatch")
            artifact = writer.seal()
            store = TimecardStore(database)
            try:
                publication = store.publish(period=period, roster_revision=roster_revision, roster_source_sha256=roster_sha, artifact=artifact, employees=captures, replace=bool(parameters.get("replace", False)), collected_at=collected_at)
                if not store.verify_projection(period.end, roster_revision, roster_sha, artifact, captures):
                    raise TimecardCollectorError("post_verification_failed")
            finally:
                store.close()
            disposition = CollectionDisposition.SKIPPED_EXISTING if publication.disposition == "already_current" else CollectionDisposition.PUBLISHED
            return CollectionReceipt(disposition, publication.run_id, 0 if disposition == CollectionDisposition.SKIPPED_EXISTING else 1, True)
        except (TimecardBrowserError, TimecardStorageError, ValueError) as exc:
            raise TimecardCollectorError(str(exc)) from exc
        finally:
            if artifact is not None and artifact.directory.exists():
                discard_artifact_run(artifact, root=artifact_root, period=period)
            if writer is not None:
                writer.cleanup()


run_timecards = collect_timecards

__all__ = ["BROWSER_REALM", "COLLECTOR_ID", "LANDING_URL", "PLUGIN_ID", "PLUGIN_RELEASE", "TimecardCollectorError", "collect_timecards", "run_timecards"]
