import json

import pytest

from companion_bridge.amazon_stream import CompanionStreamError, StreamProbeResult, build_stream_payload, build_stream_url, parse_companion_event, stream_companion_response
from companion_bridge.config import AmazonConfig
from companion_bridge.managed_session import BrowserCookie, SessionMaterial
from companion_bridge.sse import ServerSentEvent


class FakeResponse:
    status_code = 200
    headers = {"content-type": "text/event-stream"}

    def iter_text(self):
        yield 'data: ' + json.dumps({"eventType": "EVENT_DELTA", "contentType": "ANSWER", "conversationId": "conv-1", "conversationMessageId": "msg-1", "content": {"text": {"content": "hello"}}}) + "\n\n"
        yield 'data: ' + json.dumps({"contentType": "CONTROL", "content": {"control": {"controlType": "END"}}}) + "\n\n"


class FakeHttp:
    def __init__(self, **kwargs):
        self.requests = []
    def __enter__(self): return self
    def __exit__(self, *args): return None
    def stream(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return FakeResponseContext()


class FakeResponseContext:
    def __enter__(self): return FakeResponse()
    def __exit__(self, *args): return None


class Provider:
    def __init__(self): self.calls = 0
    def snapshot(self):
        self.calls += 1
        return SessionMaterial("fixture-agent", (BrowserCookie("session", "fixture-cookie", "logistics.amazon.com"),), "fixture-csrf")


def test_payload_url_and_event_parser() -> None:
    config = AmazonConfig(default_contract_types=["A"], default_program_types=["B"])
    assert build_stream_payload(prompt="hi", conversation_id="c", last_message_id="m", config=config)["context"] == {"contractTypes": ["A"], "programTypes": ["B"], "personas": ["DSP"]}
    assert build_stream_url(config.stream_endpoint, conversation_id="a/b c").endswith("/a%2Fb%20c")
    event = parse_companion_event(ServerSentEvent(data=json.dumps({"contentType": "ANSWER", "eventType": "EVENT_DELTA", "content": {"text": {"content": "hi"}}})))
    assert event.text_delta == "hi"
    assert "hi" not in json.dumps(event.to_safe_dict())


def test_direct_http_stream_starts_after_provider_snapshot() -> None:
    provider = Provider()
    clients = []
    def factory(**kwargs):
        client = FakeHttp(**kwargs)
        clients.append(client)
        return client
    events = list(stream_companion_response(prompt="fixture prompt", config=AmazonConfig(), session_provider=provider, http_client_factory=factory))
    assert provider.calls == 1
    assert [event.text_delta for event in events] == ["hello", ""]
    assert clients[0].requests[0][0] == "POST"
    assert clients[0].requests[0][2]["headers"]["anti-csrftoken-a2z"] == "fixture-csrf"
    jar = clients[0].requests[0][2]["cookies"].jar
    cookie = next(iter(jar))
    assert (cookie.name, cookie.value, cookie.domain, cookie.path) == (
        "session",
        "fixture-cookie",
        "logistics.amazon.com",
        "/",
    )


def test_stream_rejects_non_event_response_and_safe_errors() -> None:
    result = StreamProbeResult(status="failed", ok=False, stream_url="https://logistics.amazon.com/x?token=secret", error="Cookie: session-id=secret")
    rendered = json.dumps(result.to_safe_dict())
    assert "secret" not in rendered
    with pytest.raises(CompanionStreamError):
        list(stream_companion_response(prompt="x", config=AmazonConfig(companion_url="https://logistics.amazon.com/invalid"), session_provider=Provider(), http_client_factory=lambda **kwargs: FakeHttp(**kwargs)))
