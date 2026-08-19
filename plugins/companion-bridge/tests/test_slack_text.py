from companion_bridge.config import SlackConfig
from companion_bridge.slack_text import is_allowed_message, is_reset_command, message_context_from_event, neutralize_slack_mentions, prompt_from_slack_text, should_handle_thread_reply


def test_prompt_and_reset_normalization() -> None:
    assert prompt_from_slack_text("<@UBOT>   hello   Companion") == "hello Companion"
    assert is_reset_command("<@UBOT> reset")
    assert is_reset_command("start over companion")
    assert not is_reset_command("how are you")


def test_context_and_allowlist_are_deny_by_default() -> None:
    context = message_context_from_event({"team": "T1", "channel": "C1", "user": "U1", "ts": "1", "thread_ts": "0", "text": "<@UBOT> continue"})
    assert context.store_key == ("T1", "C1", "0")
    assert context.is_thread_reply and context.has_bot_mention and context.prompt == "continue"
    assert not is_allowed_message(SlackConfig(), context)
    assert is_allowed_message(SlackConfig(allowed_channels=["C1"], allowed_users=["U1"]), context)
    assert not is_allowed_message(SlackConfig(allowed_channels=["C2"], allowed_users=["U1"]), context)
    assert not is_allowed_message(SlackConfig(allowed_channels=["C1"], allowed_users=["U1"], allowed_teams=["T2"]), context)


def test_source_text_cannot_ping_slack_identities() -> None:
    rendered = neutralize_slack_mentions("hello <@U123> <!channel> <#C123>")
    assert rendered == "hello &lt;@U123> &lt;!channel> &lt;#C123>"


def test_thread_reply_policy() -> None:
    context = message_context_from_event({"team": "T1", "channel": "C1", "user": "U1", "ts": "2", "thread_ts": "1", "text": "reply"})
    assert should_handle_thread_reply(SlackConfig(allow_thread_replies_without_mention=True), context)
    assert not should_handle_thread_reply(SlackConfig(allow_thread_replies_without_mention=False), context)
