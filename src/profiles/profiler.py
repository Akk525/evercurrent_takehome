"""
Build per-user behavioural profiles from Slack activity.

No hardcoded roles. Everything is inferred from message participation,
channel membership, and the candidate events derived from threads.

Interaction weighting scheme:
    authored root message  → 3 points
    authored reply         → 2 points
    was mentioned          → 1 point

Time decay applied per interaction using an exponential with 48-hour half-life.
This makes recent activity count more than older activity.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone

from src.models import SlackWorkspace, CandidateEvent, UserContextProfile

# Interaction type weights
WEIGHT_AUTHORED_ROOT = 3.0
WEIGHT_AUTHORED_REPLY = 2.0
WEIGHT_MENTIONED = 1.0

# Time decay half-life for profile building (hours)
PROFILE_DECAY_HALF_LIFE_HOURS = 48.0


def build_user_profiles(
    workspace: SlackWorkspace,
    enriched_events: list[CandidateEvent],
    now: datetime | None = None,
) -> dict[str, UserContextProfile]:
    """
    Build a UserContextProfile for every user in the workspace.

    Returns a dict keyed by user_id.
    """
    if now is None:
        now = datetime.now(tz=timezone.utc)

    profiles: dict[str, UserContextProfile] = {}

    # Index messages by user for quick lookup
    messages_by_user: dict[str, list] = defaultdict(list)
    for msg in workspace.messages:
        messages_by_user[msg.user_id].append(msg)

    # Compute global activity level (raw message count) for normalisation
    msg_counts = {uid: len(msgs) for uid, msgs in messages_by_user.items()}
    max_msgs = max(msg_counts.values(), default=1)

    # Build interaction weights per user per thread
    interaction_by_user_thread = _compute_interaction_weights(workspace, now)

    # Normalise interaction weights: scale so global max = 1.0
    all_weights = [
        w
        for user_weights in interaction_by_user_thread.values()
        for w in user_weights.values()
    ]
    max_weight = max(all_weights, default=1.0)

    for user in workspace.users:
        uid = user.user_id
        user_messages = messages_by_user[uid]

        # Normalised interaction weights for this user
        raw_weights = interaction_by_user_thread.get(uid, {})
        normalised_weights = {
            tid: round(w / max_weight, 4)
            for tid, w in raw_weights.items()
        }

        active_channels = _active_channels(user_messages, user)
        topic_affinities = _topic_affinities_weighted(uid, enriched_events, normalised_weights)
        event_type_affinities = _event_type_affinities_weighted(uid, enriched_events, normalised_weights)
        collaborators = _frequent_collaborators(uid, workspace)
        recent_threads = _recent_threads(uid, workspace)
        activity_level = msg_counts.get(uid, 0) / max_msgs

        profiles[uid] = UserContextProfile(
            user_id=uid,
            active_channel_ids=active_channels,
            topic_affinities=topic_affinities,
            event_type_affinities=event_type_affinities,
            frequent_collaborators=collaborators,
            recent_thread_ids=recent_threads,
            activity_level=round(activity_level, 3),
            interaction_weights=normalised_weights,
        )

    return profiles


def _time_decay(timestamp: datetime, now: datetime, half_life_hours: float) -> float:
    """Exponential decay: 1.0 at now, ~0.5 at half_life_hours ago."""
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    hours_ago = max(0.0, (now - timestamp).total_seconds() / 3600.0)
    return math.pow(2.0, -hours_ago / half_life_hours)


def _compute_interaction_weights(
    workspace: SlackWorkspace,
    now: datetime,
) -> dict[str, dict[str, float]]:
    """
    For each (user, thread) pair, compute an interaction weight based on:
      - role in thread (authored root, authored reply, was mentioned)
      - time decay (recent interactions count more)

    Returns: {user_id: {thread_id: weight}}
    """
    result: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))

    # Index threads by root message for fast lookup
    thread_by_id = {t.thread_id: t for t in workspace.threads}

    for msg in workspace.messages:
        uid = msg.user_id
        tid = msg.thread_id

        decay = _time_decay(msg.timestamp, now, PROFILE_DECAY_HALF_LIFE_HOURS)

        # Determine the interaction type for the message author
        if msg.is_thread_root:
            contribution = WEIGHT_AUTHORED_ROOT * decay
        else:
            contribution = WEIGHT_AUTHORED_REPLY * decay

        result[uid][tid] += contribution

        # Mention bonus for each mentioned user
        for mentioned_uid in msg.mentions:
            if mentioned_uid != uid:
                mention_decay = _time_decay(msg.timestamp, now, PROFILE_DECAY_HALF_LIFE_HOURS)
                result[mentioned_uid][tid] += WEIGHT_MENTIONED * mention_decay

    return result


def _active_channels(user_messages: list, user) -> list[str]:
    """Channels where the user has posted, ordered by message count."""
    counts: dict[str, int] = defaultdict(int)
    for msg in user_messages:
        counts[msg.channel_id] += 1
    # Include channels from membership even without messages
    for cid in (user.channel_ids or []):
        if cid not in counts:
            counts[cid] = 0
    return sorted(counts, key=lambda c: counts[c], reverse=True)


def _topic_affinities_weighted(
    user_id: str,
    events: list[CandidateEvent],
    interaction_weights: dict[str, float],
) -> dict[str, float]:
    """
    Compute topic affinities weighted by interaction strength.

    Threads the user engaged with more deeply contribute more to their topic profile.
    """
    topic_scores: dict[str, float] = defaultdict(float)
    total = 0.0

    for event in events:
        if not event.signals:
            continue

        # Use interaction weight if available; fall back to binary participation
        weight = interaction_weights.get(event.thread_id, 0.0)
        if weight == 0.0 and user_id in event.participant_ids:
            weight = 0.1  # Minimal signal: user participated but no message-level detail

        if weight == 0.0:
            continue

        for label in event.signals.topic_labels:
            topic_scores[label] += weight
            total += weight

    if total == 0:
        return {}

    # Normalise and return top topics
    normalised = {topic: round(score / total, 3) for topic, score in topic_scores.items()}
    return dict(sorted(normalised.items(), key=lambda x: x[1], reverse=True))


def _event_type_affinities_weighted(
    user_id: str,
    events: list[CandidateEvent],
    interaction_weights: dict[str, float],
) -> dict[str, float]:
    """
    Infer which event types the user tends to engage with, weighted by interaction strength.
    """
    type_scores: dict[str, float] = defaultdict(float)
    total = 0.0

    for event in events:
        if not event.signals:
            continue

        weight = interaction_weights.get(event.thread_id, 0.0)
        if weight == 0.0 and user_id in event.participant_ids:
            weight = 0.1
        if weight == 0.0:
            continue

        dist = event.signals.event_type_dist
        type_scores["blocker"] += dist.blocker * weight
        type_scores["decision"] += dist.decision * weight
        type_scores["risk"] += dist.risk * weight
        type_scores["status_update"] += dist.status_update * weight
        type_scores["noise"] += dist.noise * weight
        total += weight

    if total == 0:
        return {}

    return {t: round(s / total, 3) for t, s in type_scores.items() if s > 0}


def _frequent_collaborators(user_id: str, workspace: SlackWorkspace) -> list[str]:
    """
    Find users who appear in the same threads most often.
    Returns top 5 collaborators sorted by co-occurrence count.
    """
    cooccurrence: dict[str, int] = defaultdict(int)

    for thread in workspace.threads:
        if user_id not in thread.participant_ids:
            continue
        for other_id in thread.participant_ids:
            if other_id != user_id:
                cooccurrence[other_id] += 1

    sorted_collabs = sorted(cooccurrence, key=lambda u: cooccurrence[u], reverse=True)
    return sorted_collabs[:5]


def _recent_threads(user_id: str, workspace: SlackWorkspace) -> list[str]:
    """Return thread IDs the user has participated in, most recent first."""
    user_threads = [
        t for t in workspace.threads
        if user_id in t.participant_ids
    ]
    user_threads.sort(key=lambda t: t.last_activity_at, reverse=True)
    return [t.thread_id for t in user_threads]
