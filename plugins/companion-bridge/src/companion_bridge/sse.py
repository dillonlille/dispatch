from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Iterator

_MAX_CHUNK_CHARS = 256 * 1024
_MAX_BUFFER_CHARS = 256 * 1024
_MAX_LINE_CHARS = 64 * 1024
_MAX_EVENT_CHARS = 1024 * 1024
_MAX_EVENT_LINES = 8192
_MAX_EVENTS = 8192


class SseDecodeError(ValueError):
    """The remote SSE stream exceeded a bounded parser contract."""


@dataclass(frozen=True)
class ServerSentEvent:
    data: str
    event: str | None = None
    event_id: str | None = None
    retry: int | None = None


class SseDecoder:
    """Incremental EventSource decoder with strict memory and event bounds."""

    def __init__(self) -> None:
        self._buffer = ""
        self._data: list[str] = []
        self._data_chars = 0
        self._line_count = 0
        self._event: str | None = None
        self._event_id: str | None = None
        self._retry: int | None = None

    def feed(self, text: str) -> list[ServerSentEvent]:
        if not isinstance(text, str) or len(text) > _MAX_CHUNK_CHARS:
            raise SseDecodeError("SSE chunk exceeded its bound")
        self._buffer += text
        events: list[ServerSentEvent] = []
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            event = self._line(line.rstrip("\r"))
            if event is not None:
                events.append(event)
        if len(self._buffer) > _MAX_BUFFER_CHARS:
            raise SseDecodeError("SSE line buffer exceeded its bound")
        return events

    def close(self) -> list[ServerSentEvent]:
        events: list[ServerSentEvent] = []
        if self._buffer:
            event = self._line(self._buffer.rstrip("\r"))
            self._buffer = ""
            if event is not None:
                events.append(event)
        event = self._dispatch()
        if event is not None:
            events.append(event)
        return events

    def _line(self, line: str) -> ServerSentEvent | None:
        if len(line) > _MAX_LINE_CHARS:
            raise SseDecodeError("SSE line exceeded its bound")
        self._line_count += 1
        if self._line_count > _MAX_EVENT_LINES:
            raise SseDecodeError("SSE event line count exceeded its bound")
        if not line:
            return self._dispatch()
        if line.startswith(":"):
            return None
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "data":
            self._data_chars += len(value) + (1 if self._data else 0)
            if self._data_chars > _MAX_EVENT_CHARS:
                raise SseDecodeError("SSE event data exceeded its bound")
            self._data.append(value)
        elif field == "event":
            self._event = value or None
        elif field == "id":
            self._event_id = value
        elif field == "retry":
            try:
                self._retry = int(value)
            except ValueError:
                self._retry = None
        return None

    def _dispatch(self) -> ServerSentEvent | None:
        if not self._data and self._event is None and self._event_id is None:
            self._retry = None
            self._line_count = 0
            return None
        event = ServerSentEvent("\n".join(self._data), self._event, self._event_id, self._retry)
        self._data = []
        self._data_chars = 0
        self._line_count = 0
        self._event = None
        self._event_id = None
        self._retry = None
        return event


def iter_sse_events(chunks: Iterable[str]) -> Iterator[ServerSentEvent]:
    decoder = SseDecoder()
    count = 0
    for chunk in chunks:
        for event in decoder.feed(chunk):
            count += 1
            if count > _MAX_EVENTS:
                raise SseDecodeError("SSE event count exceeded its bound")
            yield event
    for event in decoder.close():
        count += 1
        if count > _MAX_EVENTS:
            raise SseDecodeError("SSE event count exceeded its bound")
        yield event
