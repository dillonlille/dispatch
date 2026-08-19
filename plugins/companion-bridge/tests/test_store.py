import pytest

from companion_bridge.store import ConversationStore


def test_mapping_generation_and_dedupe(tmp_path) -> None:
    store = ConversationStore(tmp_path / "threads.sqlite3")
    first = store.upsert(team_id="T1", channel_id="C1", thread_ts="1", conversation_id="conv-1", last_message_id="msg-1", updated_at=100)
    assert store.get(team_id="T1", channel_id="C1", thread_ts="1") == first
    assert store.mark_event_processed(event_key="event-1", now=100)
    assert not store.mark_event_processed(event_key="event-1", now=101)
    generation = store.get_generation(team_id="T1", channel_id="C1", thread_ts="1")
    assert store.reset_thread(team_id="T1", channel_id="C1", thread_ts="1") == generation + 1
    assert store.upsert_if_generation(team_id="T1", channel_id="C1", thread_ts="1", conversation_id="stale", expected_generation=generation) is None
    assert store.get(team_id="T1", channel_id="C1", thread_ts="1") is None


def test_stale_cleanup_and_listing(tmp_path) -> None:
    store = ConversationStore(tmp_path / "threads.sqlite3")
    store.upsert(team_id="T1", channel_id="C1", thread_ts="old", conversation_id="old", updated_at=10)
    store.upsert(team_id="T1", channel_id="C1", thread_ts="new", conversation_id="new", updated_at=100)
    assert store.cleanup_stale(ttl_seconds=50, now=100) == 1
    assert store.list_mappings()[0].conversation_id == "new"


def test_store_creates_private_files_and_rejects_symlink(tmp_path) -> None:
    database = tmp_path / "private" / "threads.sqlite3"
    ConversationStore(database)
    assert database.parent.stat().st_mode & 0o777 == 0o700
    assert database.stat().st_mode & 0o777 == 0o600

    target = tmp_path / "target.sqlite3"
    linked = tmp_path / "linked" / "threads.sqlite3"
    linked.parent.mkdir(mode=0o700)
    linked.symlink_to(target)
    with pytest.raises(ValueError, match="unsafe"):
        ConversationStore(linked)
