import pytest

from companion_bridge.slack_stream import SlackStreamBuffer, SlackStreamClient, SlackStreamError


class FakeApi:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def api_call(self, method, *, json):
        self.calls.append((method, json))
        return self.responses.pop(0) if self.responses else {"ok": True}


def test_native_stream_payloads() -> None:
    api = FakeApi([{"ok": True, "ts": "stream-1"}, {"ok": True}, {"ok": True}, {"ok": True}])
    client = SlackStreamClient(api)
    handle = client.start_stream(channel="C1", thread_ts="1", recipient_user_id="U1", recipient_team_id="T1")
    client.append_stream(handle, "hello")
    client.stop_stream(handle, "done")
    client.post_message(channel="C1", thread_ts="1", markdown_text="fallback")
    assert [call[0] for call in api.calls] == ["chat.startStream", "chat.appendStream", "chat.stopStream", "chat.postMessage"]
    assert "markdown_text" not in api.calls[0][1]
    assert api.calls[1][1]["markdown_text"] == "hello"
    assert api.calls[3][1]["text"] == "fallback"


def test_slack_error_is_bounded() -> None:
    with pytest.raises(SlackStreamError, match="missing_scope"):
        SlackStreamClient(FakeApi([{"ok": False, "error": "missing_scope"}])).start_stream(channel="C1", thread_ts="1")


def test_buffer_flushes_by_chars() -> None:
    buffer = SlackStreamBuffer(max_chars=5)
    assert buffer.add("he") is None
    assert buffer.add("llo") == "hello"
    assert buffer.add("!") is None
    assert buffer.flush() == "!"
