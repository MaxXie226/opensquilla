"""Stable event names and open event-family patterns."""

DECLARED_EVENTS = (
    "connect.challenge",
    "agent",
    "session.message",
    "sessions.changed",
    "presence",
    "tick",
    "shutdown",
    "health",
    "heartbeat",
    "cron",
)

EVENT_PATTERNS = (
    "session.event.*",
    "task.*",
)

__all__ = ["DECLARED_EVENTS", "EVENT_PATTERNS"]
