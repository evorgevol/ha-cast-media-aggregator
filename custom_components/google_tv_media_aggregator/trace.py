"""Debug tracing of artwork lookups.

Each step is logged at DEBUG level. While a lookup is running, the step is also
recorded on that lookup's LookupTrace, which the diagnostics download shows.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime
import logging
import time
from typing import Any

from homeassistant.util import dt as dt_util

HISTORY_SIZE = 5


def format_seconds(seconds: float) -> str:
    """Format a duration such as '5h 59m', '3m 20s' or '45s'."""
    seconds = max(int(seconds), 0)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


@dataclass(slots=True)
class LookupTrace:
    """Record of one artwork lookup."""

    entity_id: str
    trigger: str
    context: dict[str, Any]
    changed: list[str] = field(default_factory=list)
    started_at: datetime = field(default_factory=dt_util.utcnow)
    steps: list[str] = field(default_factory=list)
    outcome: dict[str, Any] | None = None
    _start: float = field(default_factory=time.monotonic)
    _duration_ms: int | None = None

    def add(self, text: str) -> None:
        """Record a step."""
        elapsed = int((time.monotonic() - self._start) * 1000)
        self.steps.append(f"+{elapsed}ms {text}")

    def finish(self, status: str, **details: Any) -> None:
        """Record the outcome."""
        self._duration_ms = int((time.monotonic() - self._start) * 1000)
        self.outcome = {"status": status, **details}

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation."""
        return {
            "trigger": self.trigger,
            "changed": list(self.changed),
            "started_at": self.started_at.isoformat(),
            "duration_ms": self._duration_ms,
            "context": dict(self.context),
            "steps": list(self.steps),
            "outcome": self.outcome or {"status": "running"},
        }


_CURRENT: ContextVar[LookupTrace | None] = ContextVar(
    "google_tv_media_aggregator_lookup_trace", default=None
)


def set_trace(lookup_trace: LookupTrace | None) -> Token[LookupTrace | None]:
    """Make lookup_trace the trace for the current task."""
    return _CURRENT.set(lookup_trace)


def reset_trace(token: Token[LookupTrace | None]) -> None:
    """Restore the previous trace."""
    _CURRENT.reset(token)


def trace(logger: logging.Logger, msg: str, *args: Any) -> None:
    """Log a lookup step at DEBUG and record it on the running lookup's trace."""
    current = _CURRENT.get()
    if current is None:
        logger.debug(msg, *args, stacklevel=2)
        return
    text = msg % args if args else msg
    current.add(text)
    logger.debug("%s: %s", current.entity_id, text, stacklevel=2)
