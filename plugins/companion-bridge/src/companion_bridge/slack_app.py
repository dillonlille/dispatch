from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from .companion_client import CompanionClient
from .config import LoadedSettings, load_settings, plugin_paths
from .driver_names import DriverNameResolver, StreamingDriverIdRewriter
from .limits import ConcurrencyGate, ConcurrencyLimitError
from .redaction import RedactingFilter, redact_secrets
from .slack_stream import SlackStreamBuffer, SlackStreamClient, SlackStreamHandle
from .slack_text import SlackMessageContext, is_allowed_message, message_context_from_event, neutralize_slack_mentions, should_handle_thread_reply
from .store import ConversationStore

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class EventHandleResult:
    status: str
    conversation_id: str | None = None
    answer_chars: int = 0
    error: str | None = None


class BridgeRuntime:
    def __init__(self, *, settings: LoadedSettings, companion: CompanionClient, store: ConversationStore, slack_stream: SlackStreamClient, gate: ConcurrencyGate | None = None) -> None:
        self.settings = settings
        self.config = settings.config
        self.companion = companion
        self.store = store
        self.slack_stream = slack_stream
        self.gate = gate or ConcurrencyGate(maximum=self.config.limits.max_concurrent_streams, per_user=self.config.limits.per_user_concurrent_streams, acquire_timeout=self.config.limits.acquire_timeout_seconds)
        self._locks: dict[tuple[str, str, str], threading.RLock] = {}
        self._locks_guard = threading.Lock()
        self._last_cleanup = 0.0
        self._last_alert = 0.0
        self._alert_guard = threading.Lock()
        if self.config.driver_names.enabled:
            driver_database = settings.database_path.with_name("driver_names.sqlite3")
            self.driver_name_resolver = DriverNameResolver.from_sqlite(
                str(driver_database),
                id_regex=self.config.driver_names.id_regex,
                fallback_to_id=self.config.driver_names.fallback_to_id,
            )
        else:
            self.driver_name_resolver = DriverNameResolver.disabled(
                id_regex=self.config.driver_names.id_regex,
            )

    def handle_event(self, event: dict[str, Any], *, require_thread_reply: bool = False, ignore_mentions: bool = False, event_id: str | None = None) -> EventHandleResult:
        self._cleanup_stale_if_due()
        if _ignored_event(event):
            return EventHandleResult("ignored_bot_or_changed_message")
        context = message_context_from_event(event)
        if not _valid_context(context):
            return EventHandleResult("ignored_malformed_message")
        if require_thread_reply and not context.is_thread_reply:
            return EventHandleResult("ignored_not_thread_reply")
        if ignore_mentions and context.has_bot_mention:
            return EventHandleResult("ignored_mention_message")
        if not is_allowed_message(self.config.slack, context):
            return EventHandleResult("ignored_not_allowed")
        if not should_handle_thread_reply(self.config.slack, context):
            return EventHandleResult("ignored_thread_reply_without_mention")
        key = _event_dedupe_key(event, context, event_id)
        if not self.store.mark_event_processed(event_key=key):
            return EventHandleResult("ignored_duplicate_event")
        with self._lock_for(context):
            if context.reset_requested:
                self.store.reset_thread(team_id=context.team_id, channel_id=context.channel_id, thread_ts=context.thread_ts)
                self._send_notice(context, "DSP Companion session reset for this Slack thread.")
                return EventHandleResult("reset")
            if not context.prompt:
                self._send_notice(context, "Send a question for DSP Companion in this thread.")
                return EventHandleResult("notice")
            if len(context.prompt) > self.config.limits.max_prompt_chars:
                self._send_notice(context, "That question is too long for DSP Companion.")
                return EventHandleResult("prompt_too_long")
            try:
                with self.gate.slot(context.user_id):
                    return self._finalize_event(key, self._handle_prompt(context))
            except ConcurrencyLimitError:
                self._send_notice(context, "DSP Companion is busy. Please try again shortly.")
                return self._finalize_event(key, EventHandleResult("busy"))

    def _finalize_event(self, key: str, result: EventHandleResult) -> EventHandleResult:
        if result.status in {"busy", "stream_failed"}:
            self.store.forget_event(event_key=key)
        return result

    def _handle_prompt(self, context: SlackMessageContext) -> EventHandleResult:
        existing = self.store.get(team_id=context.team_id, channel_id=context.channel_id, thread_ts=context.thread_ts) if context.is_thread_reply else None
        generation = existing.generation if existing else self.store.get_generation(team_id=context.team_id, channel_id=context.channel_id, thread_ts=context.thread_ts)
        conversation_id = existing.conversation_id if existing else None
        last_message_id = existing.last_message_id if existing else None
        try:
            handle = self.slack_stream.start_stream(channel=context.channel_id, thread_ts=context.thread_ts, recipient_user_id=context.user_id, recipient_team_id=context.team_id or None)
        except Exception as exc:
            self._safe_post(context, "DSP Companion stream failed to start. Check bridge logs.")
            return EventHandleResult("stream_failed", error=redact_secrets(exc))
        buffer = SlackStreamBuffer(max_chars=self.config.slack.stream_buffer_chars)
        rewriter = StreamingDriverIdRewriter(self.driver_name_resolver)
        count = 0
        latest_conversation = conversation_id
        latest_message = last_message_id
        try:
            for event in self.companion.stream_response(context.prompt, conversation_id=conversation_id, last_message_id=last_message_id):
                latest_conversation = event.conversation_id or latest_conversation
                latest_message = event.conversation_message_id or latest_message
                if event.is_end:
                    break
                text = rewriter.add(event.text_delta)
                if text:
                    remaining = self.config.slack.max_slack_message_chars - count
                    safe_text = neutralize_slack_mentions(text)
                    visible = safe_text[: max(0, remaining)]
                    count += len(visible)
                    chunk = buffer.add(visible)
                    if chunk:
                        self._safe_append(handle, chunk)
                    if count >= self.config.slack.max_slack_message_chars:
                        break
            pending = rewriter.flush()
            if pending:
                remaining = self.config.slack.max_slack_message_chars - count
                safe_pending = neutralize_slack_mentions(pending)[: max(0, remaining)]
                chunk = buffer.add(safe_pending)
                count += len(safe_pending)
                if chunk:
                    self._safe_append(handle, chunk)
            final = buffer.flush()
            if final:
                self._safe_append(handle, final)
            if latest_conversation:
                self.store.upsert_if_generation(team_id=context.team_id, channel_id=context.channel_id, thread_ts=context.thread_ts, conversation_id=latest_conversation, last_message_id=latest_message, expected_generation=generation)
            self._safe_stop(handle)
            return EventHandleResult("streamed", latest_conversation, count)
        except Exception as exc:
            error = redact_secrets(exc)
            self._safe_stop(handle, self._failure_text(error))
            self._notify_admins(self._failure_category(error))
            return EventHandleResult("stream_failed", latest_conversation, count, error)

    def _send_notice(self, context: SlackMessageContext, text: str) -> None:
        try:
            handle = self.slack_stream.start_stream(channel=context.channel_id, thread_ts=context.thread_ts, initial_text=text, recipient_user_id=context.user_id, recipient_team_id=context.team_id or None)
            self._safe_stop(handle)
        except Exception:
            self._safe_post(context, text)

    def _safe_append(self, handle: SlackStreamHandle, text: str) -> None:
        try:
            self.slack_stream.append_stream(handle, text)
        except Exception as exc:
            LOGGER.warning("Slack append failed: %s", redact_secrets(exc))

    def _safe_stop(self, handle: SlackStreamHandle, text: str | None = None) -> None:
        try:
            self.slack_stream.stop_stream(handle, text)
        except Exception as exc:
            LOGGER.warning("Slack stop failed: %s", redact_secrets(exc))

    def _safe_post(self, context: SlackMessageContext, text: str) -> None:
        try:
            self.slack_stream.post_message(channel=context.channel_id, thread_ts=context.thread_ts, markdown_text=text)
        except Exception as exc:
            LOGGER.warning("Slack fallback failed: %s", redact_secrets(exc))

    def _notify_admins(self, category: str) -> None:
        destinations = list(dict.fromkeys([self.config.slack.admin_channel, *self.config.slack.admin_users]))
        destinations = [item for item in destinations if item]
        if not destinations:
            return
        with self._alert_guard:
            now = time.monotonic()
            if self._last_alert and now - self._last_alert < self.config.slack.admin_alert_cooldown_seconds:
                return
            self._last_alert = now
        for destination in destinations:
            try:
                self.slack_stream.post_channel_message(channel=destination, markdown_text=f"DSP Companion requires attention. Category: `{_safe_token(category)}`.")
            except Exception as exc:
                LOGGER.warning("Admin alert failed: %s", redact_secrets(exc))

    def _lock_for(self, context: SlackMessageContext) -> threading.RLock:
        with self._locks_guard:
            return self._locks.setdefault(context.store_key, threading.RLock())

    def _cleanup_stale_if_due(self) -> None:
        now = time.monotonic()
        if now - self._last_cleanup < self.config.sessions.cleanup_interval_seconds:
            return
        self._last_cleanup = now
        self.store.cleanup_stale(ttl_seconds=self.config.sessions.ttl_hours * 3600)

    @staticmethod
    def _failure_category(error: str) -> str:
        lowered = error.lower()
        if any(word in lowered for word in ("mfa", "captcha", "verification")):
            return "authentication_manual_action"
        if any(word in lowered for word in ("auth", "login", "credential")):
            return "authentication_required"
        if "timeout" in lowered:
            return "companion_timeout"
        return "companion_unavailable"

    @staticmethod
    def _failure_text(error: str) -> str:
        category = BridgeRuntime._failure_category(error)
        if category == "authentication_manual_action":
            return "DSP Companion needs manual authentication verification."
        if category == "authentication_required":
            return "DSP Companion needs authentication."
        if category == "companion_timeout":
            return "DSP Companion timed out. Please try again shortly."
        return "DSP Companion is temporarily unavailable. Please try again shortly."


def run(
    service_context: Any | None = None,
    *,
    app_factory: Any | None = None,
    handler_factory: Any | None = None,
) -> None:
    """Foreground Slack Socket Mode service entry point."""
    if service_context is not None and not all(
        hasattr(service_context, name)
        for name in ("should_stop", "acquire_browser_manager", "acquire_authentication_broker")
    ):
        raise ValueError("service context is invalid")
    if service_context is not None:
        paths = plugin_paths(service_context.paths)
        settings = load_settings(
            config_path=paths.config_file,
            secret_path=paths.secret_file,
            database_path=paths.database_file,
            require_tokens=True,
        )
    else:
        settings = load_settings(require_tokens=True)
    if settings.config_errors:
        raise RuntimeError("configuration is invalid")
    if settings.security_errors:
        raise RuntimeError("Slack allowlists are not configured")
    if settings.missing_required_secrets or not settings.slack_bot_token or not settings.slack_app_token:
        raise RuntimeError("Slack service credentials are not configured")
    _configure_logging()
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler
    from slack_sdk import WebClient

    app_builder = app_factory or App
    handler_builder = handler_factory or SocketModeHandler
    app = app_builder(token=settings.slack_bot_token)
    provider = None
    if service_context is not None:
        from .managed_session import ManagedCompanionSessionProvider

        authentication = service_context.acquire_authentication_broker(
            "amazon-operations",
            "default",
        )
        provider = ManagedCompanionSessionProvider(
            settings.config.amazon,
            browser_manager=service_context.acquire_browser_manager(),
            authentication_manager=authentication,
        )
    runtime = BridgeRuntime(
        settings=settings,
        companion=CompanionClient(settings.config, session_provider=provider),
        store=ConversationStore(settings.database_path),
        slack_stream=SlackStreamClient(WebClient(token=settings.slack_bot_token)),
    )

    @app.event("app_mention")
    def on_mention(event: dict[str, Any], ack: Any, body: dict[str, Any] | None = None) -> None:
        ack()
        runtime.handle_event(event, event_id=_body_event_id(body))

    @app.event("message")
    def on_message(event: dict[str, Any], ack: Any, body: dict[str, Any] | None = None) -> None:
        ack()
        runtime.handle_event(
            event,
            require_thread_reply=True,
            ignore_mentions=True,
            event_id=_body_event_id(body),
        )

    LOGGER.info("Starting DSP Companion Slack Socket Mode service")
    handler = handler_builder(app, settings.slack_app_token)
    if service_context is None:
        handler.start()
        return
    handler.connect()
    try:
        while not service_context.should_stop():
            time.sleep(0.2)
    finally:
        handler.close()


service = run


def _configure_logging() -> None:
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.addFilter(RedactingFilter())
        root.addHandler(handler)
    root.setLevel(logging.INFO)


def _body_event_id(body: dict[str, Any] | None) -> str | None:
    value = body.get("event_id") if isinstance(body, dict) else None
    return str(value) if value else None


def _ignored_event(event: dict[str, Any]) -> bool:
    return bool(event.get("bot_id") or event.get("subtype") in {"message_changed", "message_deleted", "bot_message", "channel_join", "channel_leave", "file_share"})


def _valid_context(context: SlackMessageContext) -> bool:
    values = (context.team_id, context.channel_id, context.user_id, context.message_ts, context.thread_ts)
    return all(value and len(value) <= 128 for value in values)


def _event_dedupe_key(event: dict[str, Any], context: SlackMessageContext, event_id: str | None) -> str:
    if event_id:
        key = f"event:{event_id}"
    elif event.get("client_msg_id"):
        key = f"client:{context.team_id}:{context.channel_id}:{event['client_msg_id']}"
    else:
        key = f"message:{context.team_id}:{context.channel_id}:{context.message_ts}:{event.get('subtype') or 'message'}"
    if len(key) <= 512:
        return key
    return "digest:" + hashlib.sha256(key.encode("utf-8", errors="replace")).hexdigest()


def _safe_token(value: object) -> str:
    return "".join(char if char.isalnum() or char in "_-" else "_" for char in str(value).lower())[:80] or "unknown"
