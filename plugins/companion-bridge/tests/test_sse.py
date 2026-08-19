import pytest

from companion_bridge.sse import SseDecodeError, SseDecoder, iter_sse_events


def test_chunked_multiline_event() -> None:
    decoder = SseDecoder()
    assert decoder.feed("event: message\ndata: hel") == []
    events = decoder.feed("lo\ndata: world\nid: 7\n\n")
    assert [(item.event, item.event_id, item.data) for item in events] == [("message", "7", "hello\nworld")]


def test_final_event_without_blank_line() -> None:
    assert [item.data for item in iter_sse_events(["data: one\n\n", "data: two"])] == ["one", "two"]


def test_comments_and_retry_are_safe() -> None:
    events = list(iter_sse_events([": keepalive\nretry: bad\ndata: ok\n\n"]))
    assert len(events) == 1 and events[0].data == "ok" and events[0].retry is None


def test_decoder_rejects_unterminated_oversized_input() -> None:
    decoder = SseDecoder()
    with pytest.raises(SseDecodeError):
        decoder.feed("x" * (256 * 1024 + 1))
