from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .index import IndexError, list_sections, search_fts, verify_index

ACTIONS = {"lookup", "overview", "contents", "health"}
HEALTH_PLANES = (
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


def envelope(
    *,
    ok: bool,
    action: str | None,
    status: str,
    data: dict[str, Any],
    freshness: Any = None,
    delivery: Any = None,
    error: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "action": action,
        "status": status,
        "data": data,
        "freshness": freshness,
        "delivery": delivery,
        "error": error,
    }


def error(code: str, message: str, action: str | None) -> dict[str, Any]:
    return envelope(
        ok=False,
        action=action,
        status="error" if code == "invalid_input" else "unavailable",
        data={},
        error={"code": code, "message": message},
    )


def _absolute_path(value: str | Path, label: str) -> Path:
    text = str(value).strip()
    path = Path(text)
    if not text or not path.is_absolute() or any(part in {".", ".."} for part in path.parts):
        raise IndexError(f"{label} must be an absolute path without traversal")
    if path.is_symlink():
        raise IndexError(f"{label} cannot be a symlink")
    return path.resolve(strict=False)


def configured_index(value: str | Path | None = None) -> Path | None:
    environment_configured = value is None
    raw = value if not environment_configured else os.environ.get("DISPATCH_HANDBOOK_INDEX")
    if raw is None or str(raw).strip() == "":
        return None
    path = _absolute_path(raw, "configured index")
    if environment_configured:
        root_value = os.environ.get("DISPATCH_HANDBOOK_DATA_ROOT")
        if root_value is None or root_value.strip() == "":
            raise IndexError("DISPATCH_HANDBOOK_DATA_ROOT is required with DISPATCH_HANDBOOK_INDEX")
        root = _absolute_path(root_value, "Handbook data root")
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise IndexError("configured index is outside DISPATCH_HANDBOOK_DATA_ROOT") from exc
        if path == root:
            raise IndexError("configured index must be a file below DISPATCH_HANDBOOK_DATA_ROOT")
    return path


def health(index_path: str | Path | None = None) -> dict[str, Any]:
    planes = {plane: "not_applicable" for plane in HEALTH_PLANES}
    planes["registration"] = "ready"
    planes["runtime_integrity"] = "ready"
    try:
        path = configured_index(index_path)
        if path is None:
            planes.update(
                configuration="not_configured",
                query="unavailable",
                data="unavailable",
                freshness="unavailable",
                overall="degraded",
            )
            return envelope(ok=True, action="health", status="degraded", data=planes)
        verified = verify_index(path)
        planes.update(
            configuration="ready",
            query="ready",
            data="ready",
            freshness="ready",
            overall="ready",
        )
        metadata = verified["metadata"]
        freshness = {
            "document_version": metadata["document_version"],
            "source_sha256": metadata["source_sha256"],
        }
        return envelope(
            ok=True,
            action="health",
            status="ready",
            data=planes,
            freshness=freshness,
        )
    except (OSError, IndexError, KeyError, ValueError) as exc:
        planes.update(
            configuration="invalid",
            query="unavailable",
            data="unavailable",
            freshness="unavailable",
            overall="degraded",
        )
        return envelope(
            ok=False,
            action="health",
            status="degraded",
            data=planes,
            error={"code": "index_unavailable", "message": str(exc)[:256]},
        )


def _ready_index(index_path: str | Path | None, action: str) -> tuple[Path, dict[str, Any]] | dict[str, Any]:
    try:
        path = configured_index(index_path)
        if path is None:
            return error("not_configured", "No local handbook index is configured.", action)
        verified = verify_index(path)
        return path, verified["metadata"]
    except (OSError, IndexError, KeyError, ValueError) as exc:
        return error("index_unavailable", str(exc)[:256], action)


def handle(args: Any, *, index_path: str | Path | None = None) -> dict[str, Any]:
    if type(args) is not dict:
        return error("invalid_input", "The request must be a JSON object.", None)
    action = args.get("action")
    if type(action) is not str or action not in ACTIONS:
        return error("invalid_input", "The action is not supported.", action if isinstance(action, str) else None)
    expected = {"action", "question"} if action == "lookup" else {"action"}
    if set(args) != expected:
        return error("invalid_input", "The request contains missing or unknown fields.", action)
    if action == "health":
        return health(index_path)

    question = None
    if action == "lookup":
        question = args.get("question")
        if type(question) is not str or not 3 <= len(question.strip()) <= 500:
            return error("invalid_input", "Question length must be between 3 and 500 characters.", action)
        if any(ord(character) < 32 or ord(character) == 127 for character in question):
            return error("invalid_input", "The question contains control characters.", action)

    ready = _ready_index(index_path, action)
    if isinstance(ready, dict):
        return ready
    path, metadata = ready
    freshness = {
        "document_version": metadata["document_version"],
        "source_sha256": metadata["source_sha256"],
    }
    try:
        if action == "lookup":
            hits = search_fts(path, question.strip(), limit=3)
            evidence = [
                {
                    "citation_id": hit["citation_id"],
                    "section": hit["section_title"],
                    "physical_pages": hit["physical_pages"],
                    "text": hit["text"][:4000],
                }
                for hit in hits
            ]
            return envelope(
                ok=True,
                action=action,
                status="found" if evidence else "no_match",
                data={"evidence": evidence, "synthetic": bool(metadata.get("synthetic"))},
                freshness=freshness,
            )

        inventory = list_sections(path)
        sections = inventory["sections"]
        if action == "contents":
            data = {"sections": sections, "synthetic": bool(metadata.get("synthetic"))}
        else:
            data = {
                "title": metadata.get("document_title", "Local handbook"),
                "section_count": len(sections),
                "section_titles": [section["section_title"] for section in sections],
                "synthetic": bool(metadata.get("synthetic")),
            }
        return envelope(ok=True, action=action, status="ready", data=data, freshness=freshness)
    except (OSError, IndexError, KeyError, ValueError, json.JSONDecodeError) as exc:
        return error("query_failed", str(exc)[:256], action)
