"""Core CollectorRunner for the Paycom roster."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
from typing import Any, Callable, Iterator
from zoneinfo import ZoneInfo

from collection_manager import CollectionContext, CollectionDisposition, CollectionReceipt

from .artifacts import discard_roster_artifact, stage_roster_artifact, verify_roster_artifact
from .browser import RosterBrowserError, capture_roster_export
from .models import parse_roster_source
from .period import Period, period_containing
from .storage import RosterStorageError, RosterStore

PLUGIN_ID = "paycom"
COLLECTOR_ID = "paycom-roster"
PLUGIN_RELEASE = "0.1.0"
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


@contextmanager
def _collector_lock(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    lock_path = root / ".collector.lock"
    with lock_path.open("a+b") as handle:
        os.chmod(lock_path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def collect_roster(
    context: CollectionContext,
    *,
    now: datetime | None = None,
    root: Path | None = None,
    db_path: Path | None = None,
    capture: Callable[..., bytes] = capture_roster_export,
) -> CollectionReceipt:
    """Collect one complete current roster using only ``context.session.page``."""
    page = _check_context(context)
    parameters = getattr(context, "parameters", {})
    target = parameters.get("target") if hasattr(parameters, "get") else None
    if not isinstance(target, str):
        raise RosterCollectorError("target_required")
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
                publication = store.publish(target=target, collected_at=collected_at, artifact=artifact, parsed=parsed, replace=bool(parameters.get("replace", False)))
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

__all__ = ["BROWSER_REALM", "COLLECTOR_ID", "LANDING_URL", "PLUGIN_ID", "PLUGIN_RELEASE", "RosterCollectorError", "collect_roster", "pacific_date", "run_roster"]
