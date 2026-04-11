"""
Raw Slack entity schemas.

These model the data as it would come from Slack's API (simplified for mock purposes).
All fields are optional where Slack itself might not guarantee them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class SlackMessage(BaseModel):
    message_id: str
    thread_id: str  # The ts of the parent message (same as message_id for root messages)
    channel_id: str
    user_id: str
    text: str
    timestamp: datetime
    is_thread_root: bool = False
    reaction_counts: dict[str, int] = Field(default_factory=dict)
    # User IDs mentioned with @mention in the text
    mentions: list[str] = Field(default_factory=list)
    # Reply count; only meaningful on root messages
    reply_count: int = 0
    # Users who replied (only set on root messages)
    reply_user_ids: list[str] = Field(default_factory=list)


class SlackThread(BaseModel):
    thread_id: str
    channel_id: str
    root_message_id: str
    participant_ids: list[str]
    message_ids: list[str]
    started_at: datetime
    last_activity_at: datetime
    reply_count: int = 0


class SlackUser(BaseModel):
    user_id: str
    display_name: str
    # Optional explicit role label. We do NOT rely on this for inference — it is
    # just present if the mock data explicitly encodes it.
    role: Optional[str] = None
    # Channels the user is a member of
    channel_ids: list[str] = Field(default_factory=list)


class SlackChannel(BaseModel):
    channel_id: str
    name: str
    # Broad topic area (e.g. "hardware", "firmware", "supply-chain")
    topic: Optional[str] = None
    member_ids: list[str] = Field(default_factory=list)


class SlackWorkspace(BaseModel):
    """Top-level container for all raw Slack entities."""
    users: list[SlackUser]
    channels: list[SlackChannel]
    messages: list[SlackMessage]
    threads: list[SlackThread]
