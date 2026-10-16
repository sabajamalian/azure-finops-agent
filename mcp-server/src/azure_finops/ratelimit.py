"""Sliding-window limiter for Cost Management query processing units (QPU).

Defaults follow the documented per-tenant limits: 12 QPU per 10 seconds, 60 per minute, 600 per hour.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable

DEFAULT_WINDOWS = ((10.0, 12), (60.0, 60), (3600.0, 600))


class QpuLimiter:
    def __init__(
        self,
        windows: tuple[tuple[float, int], ...] = DEFAULT_WINDOWS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.windows = windows
        self.clock = clock
        self.sleep = sleep
        self._events: deque[tuple[float, int]] = deque()
        self._lock = threading.Lock()
        self.total_waited = 0.0

    def _used(self, now: float, span: float) -> int:
        return sum(q for t, q in self._events if now - t < span)

    def wait_time(self, qpu: int) -> float:
        now = self.clock()
        longest = max(s for s, _ in self.windows)
        while self._events and now - self._events[0][0] >= longest:
            self._events.popleft()
        wait = 0.0
        for span, limit in self.windows:
            need = self._used(now, span) + qpu - limit
            if need <= 0:
                continue
            freed = 0
            for t, q in self._events:
                if now - t < span:
                    freed += q
                    if freed >= need:
                        wait = max(wait, span - (now - t))
                        break
        return wait

    def acquire(self, qpu: int = 1) -> float:
        """Block until `qpu` units fit in every window. Returns seconds waited."""
        qpu = max(1, min(qpu, min(limit for _, limit in self.windows)))
        waited = 0.0
        with self._lock:
            while True:
                w = self.wait_time(qpu)
                if w <= 0:
                    self._events.append((self.clock(), qpu))
                    self.total_waited += waited
                    return waited
                self.sleep(w)
                waited += w


def qpu_for_days(days: int) -> int:
    """Cost Management charges about 1 QPU per month of data queried."""
    return max(1, -(-days // 31))
