from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .config import SlackConfig

_MENTION = re.compile(r"<@[A-Z0-9]+(?:\|[^>]+)?>")
_RESET = re.compile(r"^(?:reset|restart|start\s+over|new\s+(?:chat|session))(?:\s+companion)?[.!\s]*$", re.I)


@dataclass(frozen=True)
class SlackMessageContext:
    team_id: str
    channel_id: str
    user_id: str
    message_ts: str
    thread_ts: str
    is_thread_reply: bool
    prompt: str
    reset_requested: bool
    has_bot_mention: bool

    @property
    def store_key(self) -> tuple[str, str, str]:
        return self.team_id, self.channel_id, self.thread_ts


def neutralize_slack_mentions(text: str) -> str:
    """Preserve ordinary markup while preventing source text from pinging Slack identities."""
    return re.sub(r"<(?=[@#!])", "&lt;", text)


def strip_bot_mentions(text: str) -> str:
    return _normalize(_MENTION.sub(" ", text or ""))


def prompt_from_slack_text(text: str) -> str:
    return strip_bot_mentions(text)


def is_reset_command(text: str) -> bool:
    return bool(_RESET.fullmatch(strip_bot_mentions(text)))


def message_context_from_event(event: dict[str, Any]) -> SlackMessageContext:
    team = str(event.get("team") or event.get("team_id") or "")
    channel = str(event.get("channel") or "")
    user = str(event.get("user") or "")
    message_ts = str(event.get("ts") or "")
    thread_ts = str(event.get("thread_ts") or message_ts)
    text = str(event.get("text") or "")
    return SlackMessageContext(team, channel, user, message_ts, thread_ts, bool(thread_ts and thread_ts != message_ts), prompt_from_slack_text(text), is_reset_command(text), bool(_MENTION.search(text)))


def is_allowed_message(config: SlackConfig, context: SlackMessageContext) -> bool:
    return bool(
        context.channel_id
        and context.user_id
        and config.allowed_channels
        and context.channel_id in config.allowed_channels
        and config.allowed_users
        and context.user_id in config.allowed_users
        and (not config.allowed_teams or context.team_id in config.allowed_teams)
    )


def should_handle_thread_reply(config: SlackConfig, context: SlackMessageContext) -> bool:
    return not context.is_thread_reply or config.allow_thread_replies_without_mention or context.has_bot_mention


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()
