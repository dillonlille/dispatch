import threading

from companion_bridge.amazon_stream import CompanionStreamEvent
from companion_bridge.config import load_settings
from companion_bridge.limits import ConcurrencyGate
from companion_bridge.slack_app import BridgeRuntime
from companion_bridge.slack_stream import SlackStreamClient
from companion_bridge.store import ConversationStore


class Api:
    def __init__(self): self.calls = []; self.counter = 0; self.lock = threading.Lock()
    def api_call(self, method, *, json):
        with self.lock:
            self.calls.append((method, json))
            if method == "chat.startStream":
                self.counter += 1
                return {"ok": True, "ts": f"stream-{self.counter}"}
            return {"ok": True}


class Companion:
    def __init__(self): self.calls = []
    def stream_response(self, prompt, *, conversation_id=None, last_message_id=None):
        self.calls.append((prompt, conversation_id, last_message_id))
        yield CompanionStreamEvent("EVENT_DELTA", "ANSWER", "conv-1", "msg-1", "req", 1, "now", text_delta="hello")
        yield CompanionStreamEvent("CONTROL", "CONTROL", "conv-1", "msg-1", "req", 2, "now", is_end=True, control_type="END")


def runtime(tmp_path):
    settings = load_settings(config_path=tmp_path / "missing.yaml", require_tokens=False)
    settings.config.slack.allowed_channels = ["C1"]
    settings.config.slack.allowed_users = ["U1", "U2"]
    api, companion = Api(), Companion()
    bridge = BridgeRuntime(settings=settings, companion=companion, store=ConversationStore(tmp_path / "threads.sqlite3"), slack_stream=SlackStreamClient(api), gate=ConcurrencyGate(maximum=2, per_user=1, acquire_timeout=0.01))
    return bridge, api, companion


def test_runtime_allowlist_stream_mapping_and_dedupe(tmp_path):
    bridge, api, companion = runtime(tmp_path)
    event = {"team": "T1", "channel": "C1", "user": "U1", "ts": "1", "text": "<@UBOT> hello"}
    first = bridge.handle_event(event, event_id="event-1")
    second = bridge.handle_event(event, event_id="event-1")
    assert first.status == "streamed" and second.status == "ignored_duplicate_event"
    assert companion.calls == [("hello", None, None)]
    assert [call[0] for call in api.calls] == ["chat.startStream", "chat.appendStream", "chat.stopStream"]


def test_runtime_continues_and_reset_generation_blocks_stale_mapping(tmp_path):
    bridge, api, companion = runtime(tmp_path)
    bridge.store.upsert(team_id="T1", channel_id="C1", thread_ts="1", conversation_id="existing", last_message_id="old")
    continued = bridge.handle_event({"team": "T1", "channel": "C1", "user": "U1", "ts": "2", "thread_ts": "1", "text": "continue"}, require_thread_reply=True, event_id="event-2")
    assert continued.status == "streamed" and companion.calls[0][1:] == ("existing", "old")
    reset = bridge.handle_event({"team": "T1", "channel": "C1", "user": "U1", "ts": "3", "thread_ts": "1", "text": "reset"}, require_thread_reply=True, ignore_mentions=True, event_id="event-3")
    assert reset.status == "reset"
    assert bridge.store.get(team_id="T1", channel_id="C1", thread_ts="1") is None


def test_runtime_unknown_thread_reply_starts_fresh_session(tmp_path):
    bridge, api, companion = runtime(tmp_path)
    result = bridge.handle_event(
        {
            "team": "T1",
            "channel": "C1",
            "user": "U1",
            "ts": "2",
            "thread_ts": "1",
            "text": "try again",
        },
        require_thread_reply=True,
        ignore_mentions=True,
        event_id="event-fresh",
    )
    assert result.status == "streamed"
    assert companion.calls == [("try again", None, None)]


def test_runtime_denies_unknown_channel_without_persisting_dedupe(tmp_path):
    bridge, api, companion = runtime(tmp_path)
    event = {"team": "T1", "channel": "C2", "user": "U1", "ts": "1", "text": "<@UBOT> hello"}
    denied = bridge.handle_event(event, event_id="event-denied")
    assert denied.status == "ignored_not_allowed" and api.calls == [] and companion.calls == []

    bridge.config.slack.allowed_channels.append("C2")
    accepted = bridge.handle_event(event, event_id="event-denied")
    assert accepted.status == "streamed"
    assert companion.calls == [("hello", None, None)]
