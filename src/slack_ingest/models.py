"""
Pydantic models for Slack Events API payloads.

Covers the subset of Slack events needed for digest engine ingestion:
  - message         (new message in channel)
  - message subtype changed / deleted
  - app_mention     (bot mentioned — useful for triggering on-demand digests)
  - app_rate_limited (delivery rate-limit signal from Slack)

Slack API references:
  https://api.slack.com/events
  https://api.slack.com/events/message
  https://api.slack.com/events/app_rate_limited
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Inner event objects (the "event" key inside the outer envelope)
# ---------------------------------------------------------------------------

class SlackMessageEvent(BaseModel):
    """
    Represents a Slack message or thread_reply event.

    The same structure covers:
      - New channel messages (thread_ts absent or == ts → root message)
      - Thread replies (thread_ts != ts)
      - Edited messages (subtype == "message_changed")
      - Deleted messages (subtype == "message_deleted")
    """
    type: str = "message"
    subtype: Optional[str] = None          # None, "message_changed", "message_deleted", "bot_message"
    channel: str                           # Slack channel ID (C...)
    user: Optional[str] = None             # Slack user ID (U...) — absent for bot messages
    text: Optional[str] = None
    ts: str                                # Slack timestamp string "1234567890.123456"
    thread_ts: Optional[str] = None        # Set when message is in a thread
    parent_user_id: Optional[str] = None   # User who started the thread
    # For message_changed subtype
    message: Optional[dict[str, Any]] = None
    previous_message: Optional[dict[str, Any]] = None

    @property
    def is_thread_reply(self) -> bool:
        return self.thread_ts is not None and self.thread_ts != self.ts

    @property
    def effective_thread_ts(self) -> str:
        """The thread root ts — same as ts for root messages."""
        return self.thread_ts if self.thread_ts else self.ts

    def to_timestamp(self) -> datetime:
        """Convert Slack ts to UTC datetime."""
        return datetime.fromtimestamp(float(self.ts), tz=timezone.utc)


class SlackAppRateLimitedEvent(BaseModel):
    """
    Emitted by Slack when event delivery to this app is rate-limited.
    The app should reduce event-driven work or back off.
    """
    type: str = "app_rate_limited"
    api_app_id: str
    minute_rate_limited: int    # Unix timestamp of the minute that was rate-limited
    token: Optional[str] = None


class SlackAppMentionEvent(BaseModel):
    """Bot mentioned in a channel — useful for triggering on-demand digests."""
    type: str = "app_mention"
    user: str
    text: str
    channel: str
    ts: str
    thread_ts: Optional[str] = None


# ---------------------------------------------------------------------------
# Outer envelope (the full Events API payload)
# ---------------------------------------------------------------------------

class SlackEventEnvelope(BaseModel):
    """
    Top-level structure of an Events API POST from Slack.

    Supports:
      - url_verification  (initial challenge handshake)
      - event_callback    (actual event delivery)
      - app_rate_limited  (rate-limit notification)
    """
    token: Optional[str] = None            # Verification token (legacy; prefer signing secret)
    team_id: Optional[str] = None
    api_app_id: Optional[str] = None
    type: str                              # "url_verification" | "event_callback" | "app_rate_limited"
    event_id: Optional[str] = None        # Unique event ID for deduplication (evt_...)
    event_time: Optional[int] = None      # Unix timestamp of event

    # For url_verification
    challenge: Optional[str] = None

    # For event_callback — the inner event object (loosely typed; specific parsing done in events.py)
    event: Optional[dict[str, Any]] = None

    # For app_rate_limited
    minute_rate_limited: Optional[int] = None


class SlackUrlVerification(BaseModel):
    """Slack URL verification challenge payload."""
    token: str
    challenge: str
    type: str = "url_verification"
