"""Core CollectorRunner for the Paycom roster."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from collection_manager import CollectionContext, CollectionDisposition, CollectionReceipt, CollectionRequest, PublicationVerification

from .artifacts import discard_roster_artifact, stage_roster_artifact, verify_roster_artifact
from .browser import RosterBrowserError, capture_roster_export
from .models import parse_roster_source
from .period import Period, period_containing
from .storage import RosterStorageError, RosterStore
from ..filesystem import exclusive_private_lock
from ..storage import StorageError, open_read_only

PLUGIN_ID = "paycom"
COLLECTOR_ID = "paycom-roster"
PLUGIN_RELEASE = "0.1.1"
BROWSER_REALM = "paycom-client"
LANDING_URL = "https://www.paycomonline.net/v4/cl/web.php/client-landing/arc"


class RosterCollectorError(RuntimeError):
    pass


def pacific_date(now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    return current.astimezone(ZoneInfo("America/Los_Angeles")).date().isoformat()


def _paths() -> tuple[Path, Path]:
    import importlib

    from dispatch_paycom.paths import PaycomPaths

    core_paths = importlib.import_module("paths")
    resolved = core_paths.DispatchPaths.from_environment()
    data = PaycomPaths.from_dispatch(resolved)
    return resolved.owner_root("cache", PLUGIN_ID) / "roster-staging", data.roster


def _check_context(context: Any) -> Any:
    session = getattr(context, "session", None)
    if session is None or getattr(session, "realm", None) != BROWSER_REALM or getattr(session, "page", None) is None:
        raise RosterCollectorError("managed_browser_session_required")
    landing = getattr(session, "landing_url", None)
    if landing != LANDING_URL:
        raise RosterCollectorError("managed_browser_session_invalid")
    return session.page


def _validated_parameters(context: Any) -> tuple[Mapping[str, object], str, bool]:
    parameters = getattr(context, "parameters", {})
    if not isinstance(parameters, Mapping):
        raise RosterCollectorError("parameters_invalid")
    unknown = set(parameters) - {"target", "replace"}
    if unknown:
        raise RosterCollectorError("unknown_parameter")
    replace = parameters.get("replace", False)
    if type(replace) is not bool:
        raise RosterCollectorError("replace_invalid")
    target = parameters.get("target")
    if not isinstance(target, str):
        raise RosterCollectorError("target_required")
    return parameters, target, replace


def verify_roster_publication(
    request: CollectionRequest, _receipt: Mapping[str, object] | None
) -> PublicationVerification:
    """Prove that no roster run for the requested target was published."""

    if request.collector_id != COLLECTOR_ID:
        raise RosterCollectorError("collector_mismatch")
    _parameters, target, _replace = _validated_parameters(request)
    try:
        period_containing(target)
    except ValueError as exc:
        raise RosterCollectorError("invalid_target") from exc
    _configured_root, database = _paths()
    if not Path(database).exists() and not Path(database).is_symlink():
        return PublicationVerification.ABSENT
    try:
        reader = open_read_only(
            database,
            required_tables=("collection_runs", "active_snapshots", "employees"),
        )
    except StorageError as exc:
        if exc.code == "not_loaded":
            return PublicationVerification.ABSENT
        raise RosterCollectorError(exc.code) from exc
    try:
        if not reader.quick_ok():
            raise RosterCollectorError("roster_integrity_failed")
        found = reader.execute("SELECT 1 FROM collection_runs WHERE target=? LIMIT 1", (target,)).fetchone()
        active = reader.execute("SELECT 1 FROM active_snapshots WHERE target=? LIMIT 1", (target,)).fetchone()
        if found is not None or active is not None:
            raise RosterCollectorError("publication_present")
        return PublicationVerification.ABSENT
    finally:
        try:
            reader.close()
        except StorageError as exc:
            raise RosterCollectorError(exc.code) from exc


def _collector_lock(root: Path):
    return exclusive_private_lock(root)


def collect_roster(
    context: CollectionContext,
    *,
    now: datetime | None = None,
    root: Path | None = None,
    db_path: Path | None = None,
    capture: Callable[..., bytes] = capture_roster_export,
) -> CollectionReceipt:
    """Collect one complete current roster using only ``context.session.page``."""
    parameters, target, replace = _validated_parameters(context)
    page = _check_context(context)
    if target != pacific_date(now):
        raise RosterCollectorError("target_not_current")
    period: Period = period_containing(target)
    configured_root, configured_db = _paths() if root is None or db_path is None else (Path(root), Path(db_path))
    artifact_root = Path(root) if root is not None else configured_root
    database = Path(db_path) if db_path is not None else configured_db
    collected_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    with _collector_lock(artifact_root):
        artifact = None
        try:
            source = capture(page, period=period, target=target)
            if not isinstance(source, bytes):
                raise RosterCollectorError("roster_export_invalid")
            parsed = parse_roster_source(source)
            artifact = stage_roster_artifact(artifact_root, target, source, parsed, collected_at)
            verify_roster_artifact(artifact)
            store = RosterStore(database)
            try:
                publication = store.publish(target=target, collected_at=collected_at, artifact=artifact, parsed=parsed, replace=replace)
                if not store.verify_projection(target, parsed, artifact.source_sha256):
                    raise RosterCollectorError("post_verification_failed")
            finally:
                store.close()
            if publication.disposition == "already_current":
                return CollectionReceipt(CollectionDisposition.SKIPPED_EXISTING, publication.run_id, 0, True)
            return CollectionReceipt(CollectionDisposition.PUBLISHED, publication.run_id, 1, True)
        except (RosterBrowserError, RosterStorageError, ValueError) as exc:
            raise RosterCollectorError(str(exc)) from exc
        finally:
            if artifact is not None and artifact.directory.exists():
                discard_roster_artifact(artifact)


run_roster = collect_roster

__all__ = ["BROWSER_REALM", "COLLECTOR_ID", "LANDING_URL", "PLUGIN_ID", "PLUGIN_RELEASE", "RosterCollectorError", "collect_roster", "pacific_date", "run_roster", "verify_roster_publication"]
