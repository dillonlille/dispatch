from __future__ import annotations

from collections.abc import Iterator

from .amazon_stream import CompanionStreamError, CompanionStreamEvent, StreamProbeResult, probe_companion_stream, stream_companion_response
from .config import BridgeConfig
from .managed_session import ManagedCompanionSessionProvider


class CompanionClient:
    """Small facade; each stream obtains a fresh Core-managed session snapshot."""

    def __init__(self, config: BridgeConfig, *, session_provider: ManagedCompanionSessionProvider | None = None) -> None:
        self.config = config
        self.session_provider = session_provider

    def stream_response(self, prompt: str, *, conversation_id: str | None = None, last_message_id: str | None = None) -> Iterator[CompanionStreamEvent]:
        yield from stream_companion_response(prompt=prompt, config=self.config.amazon, conversation_id=conversation_id, last_message_id=last_message_id, session_provider=self.session_provider)

    def probe_stream(self, prompt: str = "Reply with exactly: OK", *, conversation_id: str | None = None, last_message_id: str | None = None) -> StreamProbeResult:
        return probe_companion_stream(prompt=prompt, config=self.config.amazon, conversation_id=conversation_id, last_message_id=last_message_id, session_provider=self.session_provider)


__all__ = ["CompanionClient", "CompanionStreamError", "CompanionStreamEvent", "StreamProbeResult"]
