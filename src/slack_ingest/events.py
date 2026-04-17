"""
Slack event processing — maps incoming Slack events to local store mutations.

Entry point: process_slack_event(envelope, store, metrics)

Supported event types:
    message           → new channel message or thread reply
    message/changed   → message edit
    message/deleted   → message deletion
    app_mention       → (logged, not stored as message)
    app_rate_limited  → forwarded to rate limiter

Design:
    - All mutations go to SlackIngestStore (SQLite)
    - Dirty-thread marking is the primary mechanism for waking the reconciler
    - The digest engine never sees raw Slack events; it reads from the store
    - Idempotent: duplicate event_ids are silently dropped
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from .models import SlackEventEnvelope, SlackMessageEvent
from .store import SlackIngestStore, StoredMessage, StoredThread

logger = logging.getLogger(__name__)


def process_slack_event(
    envelope: SlackEventEnvelope,
    store: SlackIngestStore,
    metrics=None,  # Optional[SlackIngestMetrics]
) -> bool:
    """
    Process a Slack event envelope: validate, deduplicate, store, mark dirty.

    Returns True if the event was new and processed, False if it was a duplicate
    or an unsupported type.

    Called by both the HTTP Events API endpoint and the Socket Mode listener.
    """
    # Deduplication: Slack may re-deliver events on failure
    event_id = envelope.event_id
    if event_id and store.has_event(event_id):
        logger.debug("[events] Duplicate event %s — skipping", event_id)
        if metrics:
            metrics.events_deduplicated += 1
        return False

    inner = envelope.event
    if not inner:
        return False

    event_type = inner.get("type", "")
    subtype = inner.get("subtype", "")

    if metrics:
        metrics.events_received += 1

    handled = False

    if event_type == "message":
        if subtype == "message_deleted":
            handled = _handle_deleted(inner, store)
        elif subtype == "message_changed":
            handled = _handle_changed(inner, store, metrics)
        elif subtype in ("", None, "bot_message"):
            handled = _handle_new_message(inner, store, metrics)
        else:
            logger.debug("[events] Unhandled message subtype: %s", subtype)

    elif event_type == "app_mention":
        logger.info("[events] app_mention from %s in %s", inner.get("user"), inner.get("channel"))
        handled = True  # Acknowledged — no state change needed for now

    elif event_type == "app_rate_limited":
        logger.warning("[events] Slack delivered app_rate_limited event")
        if metrics:
            metrics.app_rate_limited_events += 1
        handled = True

    else:
        logger.debug("[events] Unhandled event type: %s", event_type)

    # Record event for deduplication even if not fully handled
    if event_id:
        store.record_event(
            event_id=event_id,
            event_type=event_type,
            payload=inner,
        )

    return handled


# ---------------------------------------------------------------------------
# Message handlers
# ---------------------------------------------------------------------------

def _handle_new_message(
    event: dict,
    store: SlackIngestStore,
    metrics=None,
) -> bool:
    """Process a new message or thread reply."""
    ts = event.get("ts", "")
    channel = event.get("channel", "")
    user = event.get("user", "") or event.get("bot_id", "")
    text = event.get("text", "")
    thread_ts = event.get("thread_ts")

    if not ts or not channel:
        logger.warning("[events] message event missing ts or channel: %s", event)
        return False

    is_reply = thread_ts is not None and thread_ts != ts
    effective_thread_ts = thread_ts if thread_ts else ts

    timestamp_dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
    timestamp_iso = timestamp_dt.isoformat()

    # Extract mentions (user IDs in <@U...> format)
    mentions = _extract_mentions(text)

    msg = StoredMessage(
        message_id=ts,
        thread_id=effective_thread_ts,
        channel_id=channel,
        user_id=user,
        text=text,
        timestamp=timestamp_iso,
        is_thread_root=not is_reply,
        is_deleted=False,
        is_edited=False,
        reaction_counts=json.dumps(event.get("reactions", {})),
        mentions=json.dumps(mentions),
    )
    store.upsert_message(msg)

    if is_reply:
        # Update thread record incrementally and mark dirty
        store.update_thread_activity(
            thread_id=effective_thread_ts,
            last_activity_at=timestamp_iso,
            reply_count_delta=1,
            new_participant=user or None,
            new_message_id=ts,
        )
        if metrics:
            metrics.dirty_threads_marked += 1
        logger.info(
            "[events] Thread %s marked dirty — new reply from %s in %s",
            effective_thread_ts, user, channel,
        )
    else:
        # New root message → create thread record
        thread = StoredThread(
            thread_id=ts,
            channel_id=channel,
            root_message_id=ts,
            participant_ids=json.dumps([user] if user else []),
            message_ids=json.dumps([ts]),
            started_at=timestamp_iso,
            last_activity_at=timestamp_iso,
            reply_count=0,
            is_dirty=False,
            is_complete=True,  # Root-only thread is "complete" until replies arrive
        )
        store.upsert_thread(thread)
        logger.info("[events] New root message %s in %s/%s", ts, channel, effective_thread_ts)

    if metrics:
        metrics.messages_ingested += 1

    return True


def _handle_changed(
    event: dict,
    store: SlackIngestStore,
    metrics=None,
) -> bool:
    """Process a message_changed event (edit)."""
    changed_msg = event.get("message", {})
    ts = changed_msg.get("ts", "")
    channel = event.get("channel", "")
    user = changed_msg.get("user", "")
    text = changed_msg.get("text", "")
    thread_ts = changed_msg.get("thread_ts")

    if not ts:
        return False

    effective_thread_ts = thread_ts if thread_ts else ts
    mentions = _extract_mentions(text)
    timestamp_dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)

    msg = StoredMessage(
        message_id=ts,
        thread_id=effective_thread_ts,
        channel_id=channel,
        user_id=user,
        text=text,
        timestamp=timestamp_dt.isoformat(),
        is_thread_root=(thread_ts is None or thread_ts == ts),
        is_deleted=False,
        is_edited=True,
        reaction_counts="{}",
        mentions=json.dumps(mentions),
    )
    store.upsert_message(msg)
    # Mark thread dirty so reconciler can confirm edit propagation
    store.mark_thread_dirty(effective_thread_ts)

    logger.info("[events] Message %s edited in thread %s", ts, effective_thread_ts)
    if metrics:
        metrics.dirty_threads_marked += 1
    return True


def _handle_deleted(event: dict, store: SlackIngestStore) -> bool:
    """Process a message_deleted event."""
    ts = event.get("deleted_ts", "")
    if not ts:
        return False
    store.mark_message_deleted(ts)
    logger.info("[events] Message %s marked deleted", ts)
    return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_mentions(text: str) -> list[str]:
    """Extract Slack user IDs from <@U...> mention syntax."""
    import re
    return re.findall(r"<@(U[A-Z0-9]+)>", text or "")
