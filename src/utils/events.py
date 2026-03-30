"""Pipeline event system — emits real-time events for the dashboard."""

from __future__ import annotations

import json
import logging
import queue
import time
from dataclasses import asdict, dataclass, field
from threading import Lock

logger = logging.getLogger("devops_ai_agent.events")


@dataclass
class PipelineEvent:
    """A single pipeline event."""

    stage: str
    status: str  # "pending", "running", "pass", "fail", "skipped", "consent_required"
    title: str = ""
    details: dict = field(default_factory=dict)
    timestamp: float = 0.0

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()

    def to_json(self) -> str:
        return json.dumps(asdict(self))


class EventBus:
    """Thread-safe event bus with SSE subscriber support."""

    def __init__(self):
        self._subscribers: list[queue.Queue] = []
        self._lock = Lock()
        self._history: list[PipelineEvent] = []

    def subscribe(self) -> queue.Queue:
        """Create a new subscriber queue."""
        q: queue.Queue = queue.Queue(maxsize=100)
        with self._lock:
            self._subscribers.append(q)
        # Send event history to new subscriber.
        for event in self._history:
            q.put(event)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subscribers = [s for s in self._subscribers if s is not q]

    def emit(self, event: PipelineEvent) -> None:
        """Push an event to all subscribers."""
        logger.debug("Event: %s [%s] %s", event.stage, event.status, event.title)
        with self._lock:
            self._history.append(event)
            for q in self._subscribers:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    logger.warning("Event dropped for slow subscriber: [%s] %s", event.stage, event.title[:80])

    def clear_history(self) -> None:
        with self._lock:
            self._history.clear()

    def get_history(self) -> list[PipelineEvent]:
        with self._lock:
            return list(self._history)


# Global singleton for the app.
event_bus = EventBus()
