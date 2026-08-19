from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class SlackStreamError(RuntimeError):
    pass


class SlackApiClient(Protocol):
    def api_call(self, api_method: str, *, json: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class SlackStreamHandle:
    channel: str
    ts: str
    thread_ts: str | None = None


class SlackStreamClient:
    def __init__(self, api_client: SlackApiClient) -> None:
        self.api_client = api_client

    def start_stream(self, *, channel: str, thread_ts: str, initial_text: str = "",
                     recipient_user_id: str | None = None, recipient_team_id: str | None = None) -> SlackStreamHandle:
        payload: dict[str, Any] = {"channel": channel, "thread_ts": thread_ts}
        if initial_text:
            payload["markdown_text"] = initial_text
        if recipient_user_id:
            payload["recipient_user_id"] = recipient_user_id
        if recipient_team_id:
            payload["recipient_team_id"] = recipient_team_id
        response = self._call("chat.startStream", payload)
        timestamp = response.get("ts") or response.get("stream_ts") or (response.get("message") or {}).get("ts")
        if not timestamp:
            raise SlackStreamError("chat.startStream response did not include a stream timestamp")
        return SlackStreamHandle(channel, str(timestamp), thread_ts)

    def append_stream(self, handle: SlackStreamHandle, markdown_text: str) -> None:
        if markdown_text:
            self._call("chat.appendStream", {"channel": handle.channel, "ts": handle.ts, "markdown_text": markdown_text})

    def stop_stream(self, handle: SlackStreamHandle, final_text: str | None = None) -> None:
        payload: dict[str, Any] = {"channel": handle.channel, "ts": handle.ts}
        if final_text:
            payload["markdown_text"] = final_text
        self._call("chat.stopStream", payload)

    def post_message(self, *, channel: str, thread_ts: str, markdown_text: str) -> None:
        self._call("chat.postMessage", {"channel": channel, "thread_ts": thread_ts, "text": markdown_text})

    def post_channel_message(self, *, channel: str, markdown_text: str) -> None:
        self._call("chat.postMessage", {"channel": channel, "text": markdown_text})

    def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self.api_client.api_call(method, json=payload)
        except Exception as exc:
            raise SlackStreamError(str(exc)) from exc
        if not isinstance(response, dict):
            response = getattr(response, "data", None)
        if not isinstance(response, dict):
            raise SlackStreamError(f"{method} response was not an object")
        if response.get("ok") is False:
            raise SlackStreamError(str(response.get("error") or f"{method} failed"))
        return response


class SlackStreamBuffer:
    def __init__(self, *, max_chars: int) -> None:
        self.max_chars = max(1, int(max_chars))
        self._parts: list[str] = []
        self._size = 0

    def add(self, text: str) -> str | None:
        if not text:
            return None
        self._parts.append(text)
        self._size += len(text)
        return self.flush() if self._size >= self.max_chars else None

    def flush(self) -> str | None:
        if not self._parts:
            return None
        value = "".join(self._parts)
        self._parts.clear()
        self._size = 0
        return value
