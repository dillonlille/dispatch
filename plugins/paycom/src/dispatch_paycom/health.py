"""Read-only health planes for the future Hermes-compatible Paycom boundary."""
from __future__ import annotations

from typing import Any

from .paths import coerce_paths
from .query import IDENTITY_COLUMNS, IDENTITY_TABLES, MEAL_COLUMNS, MEAL_TABLES, ROSTER_COLUMNS, ROSTER_TABLES, TIMECARD_COLUMNS, TIMECARD_TABLES
from .storage import StorageError, open_read_only

PLANES = (
    "registration",
    "runtime_integrity",
    "configuration",
    "query",
    "data",
    "freshness",
    "collector",
    "authentication",
    "service",
    "delivery",
    "overall",
)


def health(paths: Any = None) -> dict[str, Any]:
    planes = {name: "not_applicable" for name in PLANES}
    planes.update(
        registration="ready",
        runtime_integrity="ready",
        configuration="ready",
        query="ready",
        collector="not_checked",
        authentication="not_checked",
        service="not_applicable",
        delivery="not_applicable",
    )
    opened = []
    try:
        resolved = coerce_paths(paths)
        configs = (
            (resolved.roster, ROSTER_TABLES, ROSTER_COLUMNS),
            (resolved.timecards, TIMECARD_TABLES, TIMECARD_COLUMNS),
            (resolved.meals, MEAL_TABLES, MEAL_COLUMNS),
            (resolved.identity, IDENTITY_TABLES, IDENTITY_COLUMNS),
        )
        healthy = 0
        for path, tables, columns in configs:
            if not path.exists() and not path.is_symlink():
                continue
            database = open_read_only(path, required_tables=tables)
            opened.append(database)
            database.require_columns(columns)
            if database.quick_ok():
                healthy += 1
        planes["data"] = "ready" if healthy == len(configs) else "not_loaded" if healthy == 0 else "degraded"
        planes["freshness"] = "not_checked" if healthy == len(configs) else "unavailable"
        planes["overall"] = "ready" if planes["data"] == "ready" else "degraded"
        return {"status": planes["overall"], "data": planes}
    except (StorageError, OSError, ValueError) as exc:
        planes.update(configuration="invalid", query="unavailable", data="unavailable", freshness="unavailable", overall="degraded")
        return {"status": "degraded", "data": planes, "error": {"code": getattr(exc, "code", "unavailable"), "message": str(exc)[:256]}}
    finally:
        for database in opened:
            try:
                database.close()
            except StorageError:
                pass
