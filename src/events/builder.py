"""
Build CandidateEvent objects from raw Slack data.

MVP: one thread = one candidate event.
"""

from __future__ import annotations

from src.models import SlackWorkspace, CandidateEvent


def build_candidate_events(workspace: SlackWorkspace) -> list[CandidateEvent]:
    """Convert all threads in the workspace into candidate events."""
    message_by_id = {m.message_id: m for m in workspace.messages}

    events: list[CandidateEvent] = []
    for thread in workspace.threads:
        event = _thread_to_event(thread, message_by_id)
        events.append(event)

    return events


def _thread_to_event(thread, message_by_id: dict) -> CandidateEvent:
    messages = [
        message_by_id[mid]
        for mid in thread.message_ids
        if mid in message_by_id
    ]

    text_bundle = _build_text_bundle(messages)
    total_reactions = sum(
        sum(m.reaction_counts.values()) for m in messages
    )

    return CandidateEvent(
        event_id=f"evt_{thread.thread_id}",
        thread_id=thread.thread_id,
        channel_id=thread.channel_id,
        participant_ids=thread.participant_ids,
        message_ids=thread.message_ids,
        started_at=thread.started_at,
        last_activity_at=thread.last_activity_at,
        text_bundle=text_bundle,
        message_count=len(messages),
        reply_count=thread.reply_count,
        unique_participant_count=len(set(thread.participant_ids)),
        total_reactions=total_reactions,
    )


def _build_text_bundle(messages: list) -> str:
    """Concatenate messages into a flat text blob for downstream NLP."""
    parts = []
    for msg in sorted(messages, key=lambda m: m.timestamp):
        parts.append(f"[{msg.user_id}]: {msg.text}")
    return "\n".join(parts)
