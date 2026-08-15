from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .chunking import PageText, build_chunks
from .index import build_index


def _load_fixture(path: Path) -> tuple[dict[str, Any], bytes]:
    raw = path.read_bytes()
    payload = json.loads(raw)
    if type(payload) is not dict or set(payload) != {
        "schema_version",
        "synthetic",
        "provenance",
        "document_id",
        "title",
        "version",
        "sections",
    }:
        raise ValueError("synthetic fixture shape is invalid")
    if payload["schema_version"] != 1 or payload["synthetic"] is not True:
        raise ValueError("fixture is not explicitly synthetic")
    if type(payload["provenance"]) is not str or not payload["provenance"].startswith("Wholly invented"):
        raise ValueError("fixture provenance is invalid")
    if type(payload["document_id"]) is not str or not payload["document_id"].startswith("synthetic-"):
        raise ValueError("fixture document ID is invalid")
    if type(payload["sections"]) is not list or not payload["sections"]:
        raise ValueError("synthetic fixture has no sections")
    return payload, raw


def build_demo(fixture: Path, target: Path) -> dict[str, Any]:
    payload, raw = _load_fixture(fixture)
    pages = []
    for number, section in enumerate(payload["sections"], 1):
        if type(section) is not dict or set(section) != {"id", "title", "text"}:
            raise ValueError("synthetic section shape is invalid")
        if not all(type(section[key]) is str and section[key].strip() for key in section):
            raise ValueError("synthetic section contains an invalid value")
        pages.append(
            PageText(
                physical_page=number,
                section_id=section["id"],
                section_title=section["title"],
                source_kind="synthetic_fixture",
                language="en",
                text=section["text"],
                printed_page_label=str(number),
            )
        )
    source_sha256 = hashlib.sha256(raw).hexdigest()
    chunks = build_chunks(
        pages,
        document_version=payload["version"],
        source_sha256=source_sha256,
        target_words=100,
        overlap_words=0,
        max_words=100,
    )
    return build_index(
        target,
        chunks,
        metadata={
            "document_id": payload["document_id"],
            "document_title": payload["title"],
            "document_version": payload["version"],
            "source_sha256": source_sha256,
            "synthetic": True,
        },
    )
