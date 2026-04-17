"""
Adapter: converts SlackIngestStore data to SlackWorkspace for the digest engine.

Used by api/server.py when Slack integration is active:
    if store.has_data():
        workspace = load_workspace_from_slack_store(store)
    else:
        workspace = load_workspace(DATA_DIR)  # mock fallback

This keeps the digest engine completely unchanged — it only ever sees SlackWorkspace.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from src.models.raw import (
    SlackChannel,
    SlackMessage,
    SlackThread,
    SlackUser,
    SlackWorkspace,
)

from .store import SlackIngestStore, StoredThread

logger = logging.getLogger(__name__)


def _parse_json_list(raw: str, field_name: str) -> list:
    """Deserialise a JSON list field; returns [] on any error."""
    try:
        result = json.loads(raw)
        if isinstance(result, list):
            return result
        logger.warning("Expected JSON list for %s, got %s", field_name, type(result))
        return []
    except (json.JSONDecodeError, TypeError):
        logger.warning("Failed to parse JSON list for field %s: %r", field_name, raw)
        return []


def _parse_datetime(raw: str, field_name: str) -> datetime:
    """Parse an ISO datetime string; falls back to epoch on failure."""
    try:
        return datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        logger.warning("Failed to parse datetime for %s: %r", field_name, raw)
        return datetime.fromtimestamp(0)


def _parse_json_dict(raw: str, field_name: str) -> dict:
    """Deserialise a JSON dict field; returns {} on any error."""
    try:
        result = json.loads(raw)
        if isinstance(result, dict):
            return result
        logger.warning("Expected JSON dict for %s, got %s", field_name, type(result))
        return {}
    except (json.JSONDecodeError, TypeError):
        logger.warning("Failed to parse JSON dict for field %s: %r", field_name, raw)
        return {}


def load_workspace_from_slack_store(store: SlackIngestStore) -> SlackWorkspace:
    """
    Convert all data in a SlackIngestStore into a SlackWorkspace.

    Returns a minimal empty workspace if the store has no data — never raises.
    """
    # Build a thread lookup so we can populate reply_count on root messages
    stored_threads: list[StoredThread] = []
    try:
        stored_threads = store.list_threads()
    except Exception:
        logger.exception("Failed to list threads from store; continuing with empty list")

    thread_reply_count: dict[str, int] = {t.thread_id: t.reply_count for t in stored_threads}

    # ------------------------------------------------------------------ #
    # Messages                                                             #
    # ------------------------------------------------------------------ #
    slack_messages: list[SlackMessage] = []
    try:
        for sm in store.get_all_messages(exclude_deleted=True):
            try:
                reaction_counts = _parse_json_dict(sm.reaction_counts, "reaction_counts")
                mentions = _parse_json_list(sm.mentions, "mentions")
                timestamp = _parse_datetime(sm.timestamp, "timestamp")

                # Populate reply_count from thread metadata for root messages
                reply_count = 0
                if sm.is_thread_root:
                    reply_count = thread_reply_count.get(sm.thread_id, 0)

                slack_messages.append(
                    SlackMessage(
                        message_id=sm.message_id,
                        thread_id=sm.thread_id,
                        channel_id=sm.channel_id,
                        user_id=sm.user_id,
                        text=sm.text,
                        timestamp=timestamp,
                        is_thread_root=bool(sm.is_thread_root),
                        reaction_counts=reaction_counts,
                        mentions=mentions,
                        reply_count=reply_count,
                    )
                )
            except Exception:
                logger.exception("Failed to map StoredMessage %s; skipping", sm.message_id)
    except Exception:
        logger.exception("Failed to retrieve messages from store; returning empty list")

    # ------------------------------------------------------------------ #
    # Threads                                                              #
    # ------------------------------------------------------------------ #
    slack_threads: list[SlackThread] = []
    for st in stored_threads:
        try:
            participant_ids = _parse_json_list(st.participant_ids, "participant_ids")
            message_ids = _parse_json_list(st.message_ids, "message_ids")
            started_at = _parse_datetime(st.started_at, "started_at")
            last_activity_at = _parse_datetime(st.last_activity_at, "last_activity_at")

            slack_threads.append(
                SlackThread(
                    thread_id=st.thread_id,
                    channel_id=st.channel_id,
                    root_message_id=st.root_message_id,
                    participant_ids=participant_ids,
                    message_ids=message_ids,
                    started_at=started_at,
                    last_activity_at=last_activity_at,
                    reply_count=st.reply_count,
                )
            )
        except Exception:
            logger.exception("Failed to map StoredThread %s; skipping", st.thread_id)

    # ------------------------------------------------------------------ #
    # Channels                                                             #
    # ------------------------------------------------------------------ #
    slack_channels: list[SlackChannel] = []
    try:
        for sc in store.list_channels():
            try:
                member_ids = _parse_json_list(sc.member_ids, "member_ids")
                slack_channels.append(
                    SlackChannel(
                        channel_id=sc.channel_id,
                        name=sc.name,
                        topic=sc.topic,
                        member_ids=member_ids,
                    )
                )
            except Exception:
                logger.exception("Failed to map StoredChannel %s; skipping", sc.channel_id)
    except Exception:
        logger.exception("Failed to retrieve channels from store; returning empty list")

    # ------------------------------------------------------------------ #
    # Users                                                                #
    # ------------------------------------------------------------------ #
    slack_users: list[SlackUser] = []
    try:
        for su in store.list_users():
            try:
                # real_name preferred for display; fall back to display_name
                display_name = su.real_name or su.display_name

                slack_users.append(
                    SlackUser(
                        user_id=su.user_id,
                        display_name=display_name,
                        # Slack API does not surface role — leave as None
                        role=None,
                        # channel_ids are not stored per-user in the ingest store;
                        # the digest engine builds this from channel.member_ids
                        channel_ids=[],
                    )
                )
            except Exception:
                logger.exception("Failed to map StoredUser %s; skipping", su.user_id)
    except Exception:
        logger.exception("Failed to retrieve users from store; returning empty list")

    return SlackWorkspace(
        users=slack_users,
        channels=slack_channels,
        messages=slack_messages,
        threads=slack_threads,
    )
