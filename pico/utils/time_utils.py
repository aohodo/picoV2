"""Timezone-aware timestamps shared by persisted runtime state."""

from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat()
