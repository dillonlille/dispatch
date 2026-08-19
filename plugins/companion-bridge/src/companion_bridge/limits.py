from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from collections import defaultdict
from collections.abc import Iterator


class ConcurrencyLimitError(RuntimeError):
    """A bounded request cannot obtain a stream slot."""


class ConcurrencyGate:
    """Global and per-user limits with a bounded wait, never an unbounded queue."""

    def __init__(self, *, maximum: int, per_user: int, acquire_timeout: float = 1.0) -> None:
        self.maximum = max(1, int(maximum))
        self.per_user = max(1, int(per_user))
        self.acquire_timeout = max(0.0, float(acquire_timeout))
        self._global = threading.BoundedSemaphore(self.maximum)
        self._lock = threading.Lock()
        self._active: defaultdict[str, int] = defaultdict(int)
        self._condition = threading.Condition(self._lock)

    @contextmanager
    def slot(self, user_id: str) -> Iterator[None]:
        deadline = time.monotonic() + self.acquire_timeout
        with self._condition:
            while self._active[user_id] >= self.per_user:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ConcurrencyLimitError("per-user stream capacity is busy")
                self._condition.wait(timeout=remaining)
            self._active[user_id] += 1
        remaining = max(0.0, deadline - time.monotonic())
        if not self._global.acquire(timeout=remaining):
            with self._condition:
                self._active[user_id] -= 1
                if not self._active[user_id]:
                    del self._active[user_id]
                self._condition.notify_all()
            raise ConcurrencyLimitError("global stream capacity is busy")
        try:
            yield
        finally:
            self._global.release()
            with self._condition:
                self._active[user_id] -= 1
                if not self._active[user_id]:
                    del self._active[user_id]
                self._condition.notify_all()

    @property
    def active_count(self) -> int:
        with self._lock:
            return sum(self._active.values())
