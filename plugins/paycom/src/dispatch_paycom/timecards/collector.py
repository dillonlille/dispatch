"""Core CollectorRunner for complete Paycom timecards."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from collection_manager import CollectionContext, CollectionDisposition, CollectionReceipt, CollectionRequest, PublicationVerification

from dispatch_paycom.roster.storage import RosterStorageError, read_active_roster
from dispatch_paycom.storage import StorageError, open_read_only
from .artifacts import TimecardArtifact, TimecardArtifactWriter, discard_artifact_run
from .browser import TimecardBrowserError, TimecardCapture, capture_timecard
from .period import Period, period_from_end
from .storage import TimecardStorageError, TimecardStore
from ..filesystem import exclusive_private_lock

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


def _validated_parameters(context: Any) -> tuple[Mapping[str, object], str, bool]:
    parameters = getattr(context, "parameters", {})
    if not isinstance(parameters, Mapping):
        raise TimecardCollectorError("parameters_invalid")
    unknown = set(parameters) - {"period_end", "period-end", "target", "replace"}
    if unknown:
        raise TimecardCollectorError("unknown_parameter")
    replace = parameters.get("replace", False)
    if type(replace) is not bool:
        raise TimecardCollectorError("replace_invalid")
    period_keys = [key for key in ("period_end", "period-end", "target") if key in parameters]
    if len(period_keys) > 1:
        raise TimecardCollectorError("period_ambiguous")
    period_end = parameters.get(period_keys[0]) if period_keys else None
    if not isinstance(period_end, str):
        raise TimecardCollectorError("period_end_required")
    return parameters, period_end, replace


def verify_timecard_publication(
    request: CollectionRequest, _receipt: Mapping[str, object] | None
) -> PublicationVerification:
    """Prove that no run for the requested period was published."""

    if request.collector_id != COLLECTOR_ID:
        raise TimecardCollectorError("collector_mismatch")
    _parameters, period_end, _replace = _validated_parameters(request)
    try:
        period = period_from_end(period_end)
    except ValueError as exc:
        raise TimecardCollectorError("invalid_period") from exc
    _configured_root, database, _roster = _paths()
    if not Path(database).exists() and not Path(database).is_symlink():
        return PublicationVerification.ABSENT
    try:
        reader = open_read_only(
            database,
            required_tables=("runs", "active_periods", "employee_timecards", "days", "punches", "weekly_totals"),
        )
    except StorageError as exc:
        if exc.code == "not_loaded":
            return PublicationVerification.ABSENT
        raise TimecardCollectorError(exc.code) from exc
    try:
        if not reader.quick_ok():
            raise TimecardCollectorError("timecard_integrity_failed")
        found = reader.execute("SELECT 1 FROM runs WHERE period_end=? LIMIT 1", (period.end,)).fetchone()
        active = reader.execute("SELECT 1 FROM active_periods WHERE period_end=? LIMIT 1", (period.end,)).fetchone()
        if found is not None or active is not None:
            raise TimecardCollectorError("publication_present")
        return PublicationVerification.ABSENT
    finally:
        try:
            reader.close()
        except StorageError as exc:
            raise TimecardCollectorError(exc.code) from exc


def _lock(root: Path):
    return exclusive_private_lock(root)


def collect_timecards(
    context: CollectionContext,
    *,
    root: Path | None = None,
    db_path: Path | None = None,
    roster_path: Path | None = None,
    capture: Callable[..., TimecardCapture] = capture_timecard,
) -> CollectionReceipt:
    parameters, period_end, replace = _validated_parameters(context)
    page = _page(context)
    try:
        period: Period = period_from_end(period_end)
    except ValueError as exc:
        raise TimecardCollectorError("invalid_period") from exc
    configured_root, configured_db, configured_roster = _paths() if root is None or db_path is None or roster_path is None else (Path(root), Path(db_path), Path(roster_path))
    artifact_root = Path(root) if root is not None else configured_root
    database = Path(db_path) if db_path is not None else configured_db
    roster_database = Path(roster_path) if roster_path is not None else configured_roster
    try:
        roster_binding = read_active_roster(roster_database)
    except (RosterStorageError, OSError) as exc:
        raise TimecardCollectorError(str(exc)) from exc
    collected_at = datetime.now(timezone.utc).isoformat()
    writer: TimecardArtifactWriter | None = None
    artifact: TimecardArtifact | None = None
    with _lock(artifact_root):
        roster_revision = roster_binding.revision
        roster_sha = roster_binding.source_sha256
        employees = roster_binding.employees
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
                publication = store.publish(period=period, roster_revision=roster_revision, roster_source_sha256=roster_sha, artifact=artifact, employees=captures, replace=replace, collected_at=collected_at)
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

__all__ = ["BROWSER_REALM", "COLLECTOR_ID", "LANDING_URL", "PLUGIN_ID", "PLUGIN_RELEASE", "TimecardCollectorError", "collect_timecards", "run_timecards", "verify_timecard_publication"]
