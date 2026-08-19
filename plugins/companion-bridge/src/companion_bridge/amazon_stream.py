from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Callable, Iterator
from urllib.parse import quote, urlsplit

import httpx

from .config import AmazonConfig
from .managed_session import ManagedCompanionSessionProvider, SessionMaterial, validate_companion_config
from .redaction import redact_secrets
from .sse import ServerSentEvent, SseDecodeError, iter_sse_events


class CompanionStreamError(RuntimeError):
    pass


@dataclass(frozen=True)
class CompanionStreamEvent:
    event_type: str | None
    content_type: str | None
    conversation_id: str | None
    conversation_message_id: str | None
    request_id: str | None
    global_sequence_number: int | None
    timestamp: str | None
    text_delta: str = ""
    control_type: str | None = None
    is_end: bool = False
    sse_event: str | None = None
    parse_error: str | None = None

    def to_safe_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result.pop("text_delta", None)
        result["text_delta_chars"] = len(self.text_delta)
        if result.get("parse_error"):
            result["parse_error"] = redact_secrets(result["parse_error"])
        return result


@dataclass(frozen=True)
class StreamProbeResult:
    status: str
    ok: bool
    stream_url: str
    http_status: int | None = None
    content_type: str | None = None
    event_count: int = 0
    answer_event_count: int = 0
    answer_chars: int = 0
    conversation_id: str | None = None
    conversation_message_id: str | None = None
    error: str | None = None

    def to_safe_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["stream_url"] = _safe_url(self.stream_url)
        if result.get("error"):
            result["error"] = redact_secrets(result["error"])
        return result


def stream_companion_response(*, prompt: str, config: AmazonConfig,
                              conversation_id: str | None = None,
                              last_message_id: str | None = None,
                              session_provider: ManagedCompanionSessionProvider | None = None,
                              http_client_factory: Callable[..., Any] = httpx.Client) -> Iterator[CompanionStreamEvent]:
    """Snapshot a managed session, release its lease, then stream with direct HTTP."""
    try:
        validate_companion_config(config)
    except Exception as exc:
        raise CompanionStreamError(str(exc)) from exc
    provider = session_provider or ManagedCompanionSessionProvider(config)
    try:
        material = provider.snapshot()
    except Exception as exc:
        if isinstance(exc, CompanionStreamError):
            raise
        raise CompanionStreamError(redact_secrets(str(exc))) from exc
    stream_url = build_stream_url(config.stream_endpoint, conversation_id=conversation_id)
    headers = _headers(material, config.csrf_header, config.companion_url)
    cookies = _cookie_jar(material)
    payload = build_stream_payload(prompt=prompt, conversation_id=conversation_id, last_message_id=last_message_id, config=config)
    try:
        with http_client_factory(timeout=config.request_timeout_seconds, follow_redirects=False) as client:
            with client.stream("POST", stream_url, headers=headers, cookies=cookies, json=payload) as response:
                status = int(response.status_code)
                content_type = str(response.headers.get("content-type", ""))
                location = str(response.headers.get("location", ""))
                if status in {401, 403} or "amazon.com/ap/signin" in location:
                    raise CompanionStreamError("Companion stream requires authentication")
                if status < 200 or status >= 300:
                    raise CompanionStreamError(f"Companion stream returned HTTP {status}")
                if "text/event-stream" not in content_type.lower():
                    raise CompanionStreamError("Companion stream did not return text/event-stream")
                for event in parse_companion_sse(response.iter_text()):
                    yield event
                    if event.is_end:
                        break
    except httpx.HTTPError as exc:
        raise CompanionStreamError(redact_secrets(str(exc))) from exc
    except SseDecodeError as exc:
        raise CompanionStreamError("Companion stream exceeded its bounded SSE contract") from exc


def probe_companion_stream(*, prompt: str, config: AmazonConfig, **kwargs: Any) -> StreamProbeResult:
    result = StreamProbeResult(status="stream_empty", ok=False, stream_url=build_stream_url(config.stream_endpoint, conversation_id=kwargs.get("conversation_id")))
    try:
        for event in stream_companion_response(prompt=prompt, config=config, **kwargs):
            result = StreamProbeResult(
                status="stream_ok", ok=True, stream_url=result.stream_url,
                event_count=result.event_count + 1,
                answer_event_count=result.answer_event_count + bool(event.text_delta),
                answer_chars=result.answer_chars + len(event.text_delta),
                conversation_id=event.conversation_id or result.conversation_id,
                conversation_message_id=event.conversation_message_id or result.conversation_message_id,
            )
    except CompanionStreamError as exc:
        return StreamProbeResult(status="stream_failed", ok=False, stream_url=result.stream_url, error=str(exc), event_count=result.event_count, answer_event_count=result.answer_event_count, answer_chars=result.answer_chars)
    return result


def parse_companion_sse(chunks: Iterator[str]) -> Iterator[CompanionStreamEvent]:
    for event in iter_sse_events(chunks):
        yield parse_companion_event(event)


def parse_companion_event(event: ServerSentEvent) -> CompanionStreamEvent:
    try:
        payload = json.loads(event.data) if event.data else {}
    except (TypeError, ValueError) as exc:
        return CompanionStreamEvent(None, None, None, None, None, None, None, sse_event=event.event, parse_error=str(exc))
    if not isinstance(payload, dict):
        return CompanionStreamEvent(None, None, None, None, None, None, None, sse_event=event.event, parse_error="Companion SSE payload was not an object")
    content_type = _optional_str(payload.get("contentType"))
    control = _control_type(payload) if content_type == "CONTROL" else None
    return CompanionStreamEvent(
        event_type=_optional_str(payload.get("eventType")),
        content_type=content_type,
        conversation_id=_optional_str(payload.get("conversationId")),
        conversation_message_id=_optional_str(payload.get("conversationMessageId")),
        request_id=_optional_str(payload.get("requestId")),
        global_sequence_number=_optional_int(payload.get("globalSequenceNumber")),
        timestamp=_optional_str(payload.get("timestamp")),
        text_delta=_text_delta(payload) if content_type == "ANSWER" else "",
        control_type=control,
        is_end=(control or "").upper() in {"END", "DONE", "FINISH", "FINISHED"},
        sse_event=event.event,
    )


def build_stream_payload(*, prompt: str, conversation_id: str | None, last_message_id: str | None, config: AmazonConfig) -> dict[str, Any]:
    return {"content": prompt, "conversationId": conversation_id, "context": {"contractTypes": list(config.default_contract_types), "programTypes": list(config.default_program_types), "personas": [config.default_persona] if config.default_persona else []}, "lastMessageId": last_message_id}


def build_stream_url(endpoint: str, *, conversation_id: str | None) -> str:
    if conversation_id:
        return endpoint.rstrip("/") + "/" + quote(conversation_id, safe="")
    return endpoint.rstrip("/")


def _cookie_jar(material: SessionMaterial) -> httpx.Cookies:
    jar = httpx.Cookies()
    for cookie in material.cookies:
        jar.set(cookie.name, cookie.value, domain=cookie.domain, path=cookie.path)
    return jar


def _headers(material: SessionMaterial, csrf_header: str, companion_url: str) -> dict[str, str]:
    return {"accept": "text/event-stream", "content-type": "application/json", "origin": "https://logistics.amazon.com", "referer": companion_url, "user-agent": material.user_agent, csrf_header: material.csrf_token}


def _text_delta(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except ValueError:
            return content
    if isinstance(content, dict) and isinstance(content.get("text"), dict) and isinstance(content["text"].get("content"), str):
        return content["text"]["content"]
    return ""


def _control_type(payload: dict[str, Any]) -> str | None:
    content = payload.get("content")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except ValueError:
            return None
    if isinstance(content, dict):
        control = content.get("control")
        if isinstance(control, dict):
            return next((str(control[key]) for key in ("controlType", "type", "name") if control.get(key)), None)
        if content.get("controlType"):
            return str(content["controlType"])
    return None


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_url(value: str) -> str:
    parsed = urlsplit(value)
    return parsed._replace(query="", fragment="").geturl()
