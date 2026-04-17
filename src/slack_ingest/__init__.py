"""
Slack ingestion layer — event-driven local data collection.

Primary path:
    Socket Mode or Events API (HTTP) → process_slack_event() → SlackIngestStore

Repair path:
    ReconciliationWorker → targeted conversations.replies fetches → SlackIngestStore

The digest engine reads from SlackIngestStore (or the mock workspace) — it never
touches the Slack API directly.

Gracefully unavailable when Slack is not configured.
"""

from .store import SlackIngestStore
from .events import process_slack_event
from .rate_limits import RateLimiter, SlackMethod
from .reconciler import ReconciliationWorker
from .mapping import UserIdentityMap
from .adapter import load_workspace_from_slack_store

__all__ = [
    "SlackIngestStore",
    "process_slack_event",
    "RateLimiter",
    "SlackMethod",
    "ReconciliationWorker",
    "UserIdentityMap",
    "load_workspace_from_slack_store",
]
