"""Deterministic physical-page-aware child chunk construction."""
from __future__ import annotations

from dataclasses import dataclass, replace
import re
from typing import Iterable

@dataclass(frozen=True)
class PageText:
    """One synthetic or operator-supplied text page with citation metadata."""

    physical_page: int
    section_id: str
    section_title: str
    source_kind: str
    language: str
    text: str
    printed_page_label: str | None = None



class ChunkingError(ValueError):
    """Input pages cannot be chunked without losing citation identity."""


_TOKEN = re.compile(r"\S+")


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    citation_id: str
    section_id: str
    section_title: str
    source_kind: str
    language: str
    physical_pages: tuple[int, ...]
    printed_page_labels: tuple[str, ...]
    text: str
    word_count: int
    previous_chunk_id: str | None = None
    next_chunk_id: str | None = None


@dataclass(frozen=True)
class _Token:
    value: str
    physical_page: int
    printed_page_label: str | None


def _version_id(version: str) -> str:
    match = re.search(r"\b(20\d{2})\b", version)
    return match.group(1) if match else re.sub(r"[^A-Za-z0-9]+", "", version)[:12]


def _tokens(page: PageText) -> list[_Token]:
    return [
        _Token(token, page.physical_page, page.printed_page_label)
        for token in _TOKEN.findall(page.text)
    ]


def build_chunks(
    pages: Iterable[PageText],
    *,
    document_version: str,
    source_sha256: str,
    target_words: int = 360,
    overlap_words: int = 48,
    max_words: int = 450,
) -> list[Chunk]:
    values = list(pages)
    if not values:
        raise ChunkingError("at least one extracted page is required")
    physical = [page.physical_page for page in values]
    if physical != sorted(physical) or len(physical) != len(set(physical)):
        raise ChunkingError("extracted physical pages must be unique and ordered")
    if not (100 <= target_words <= max_words <= 600):
        raise ChunkingError("invalid child chunk bounds")
    if not 0 <= overlap_words < target_words:
        raise ChunkingError("invalid chunk overlap")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", source_sha256):
        raise ChunkingError("source hash must be SHA-256")

    version = _version_id(document_version)
    result: list[Chunk] = []
    index = 0
    while index < len(values):
        section_id = values[index].section_id
        section_pages: list[PageText] = []
        while index < len(values) and values[index].section_id == section_id:
            section_pages.append(values[index])
            index += 1
        first = section_pages[0]
        if any(
            page.section_title != first.section_title
            or page.source_kind != first.source_kind
            or page.language != first.language
            for page in section_pages
        ):
            raise ChunkingError("section metadata changed inside one section")
        section_tokens = [token for page in section_pages for token in _tokens(page)]
        if not section_tokens:
            continue
        start = 0
        ordinal = 1
        section_chunks: list[Chunk] = []
        while start < len(section_tokens):
            end = min(start + target_words, len(section_tokens))
            remaining = len(section_tokens) - end
            if 0 < remaining < max(80, overlap_words) and len(section_tokens) - start <= max_words:
                end = len(section_tokens)
            window = section_tokens[start:end]
            pages_tuple = tuple(dict.fromkeys(token.physical_page for token in window))
            labels = tuple(dict.fromkeys(
                token.printed_page_label for token in window if token.printed_page_label
            ))
            page_component = (
                f"p{pages_tuple[0]:03d}"
                if len(pages_tuple) == 1
                else f"p{pages_tuple[0]:03d}-{pages_tuple[-1]:03d}"
            )
            chunk_id = f"hb{version}-{section_id}-{ordinal:03d}"
            citation_id = f"HB{version}-{page_component}-{section_id}-{ordinal:03d}"
            section_chunks.append(Chunk(
                chunk_id=chunk_id,
                citation_id=citation_id,
                section_id=section_id,
                section_title=first.section_title,
                source_kind=first.source_kind,
                language=first.language,
                physical_pages=pages_tuple,
                printed_page_labels=labels,
                text=" ".join(token.value for token in window),
                word_count=len(window),
            ))
            if end == len(section_tokens):
                break
            start = end - overlap_words
            ordinal += 1
        for offset, chunk in enumerate(section_chunks):
            section_chunks[offset] = replace(
                chunk,
                previous_chunk_id=section_chunks[offset - 1].chunk_id if offset else None,
                next_chunk_id=section_chunks[offset + 1].chunk_id if offset + 1 < len(section_chunks) else None,
            )
        result.extend(section_chunks)
    identifiers = [chunk.chunk_id for chunk in result]
    if len(identifiers) != len(set(identifiers)):
        raise ChunkingError("chunk identifiers are not unique")
    return result
