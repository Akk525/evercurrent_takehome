"""
Build per-user behavioural profiles from Slack activity.

No hardcoded roles. Everything is inferred from message participation,
channel membership, and the candidate events derived from threads.
"""

from __future__ import annotations

from collections import defaultdict

from src.models import SlackWorkspace, CandidateEvent, UserContextProfile


def build_user_profiles(
    workspace: SlackWorkspace,
    enriched_events: list[CandidateEvent],
) -> dict[str, UserContextProfile]:
    """
    Build a UserContextProfile for every user in the workspace.

    Returns a dict keyed by user_id.
    """
    profiles: dict[str, UserContextProfile] = {}

    # Index messages by user for quick lookup
    messages_by_user: dict[str, list] = defaultdict(list)
    for msg in workspace.messages:
        messages_by_user[msg.user_id].append(msg)

    # Compute global activity level (message count) for normalisation
    msg_counts = {uid: len(msgs) for uid, msgs in messages_by_user.items()}
    max_msgs = max(msg_counts.values(), default=1)

    for user in workspace.users:
        uid = user.user_id
        user_messages = messages_by_user[uid]

        active_channels = _active_channels(user_messages, user)
        topic_affinities = _topic_affinities(uid, enriched_events)
        event_type_affinities = _event_type_affinities(uid, enriched_events)
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
        )

    return profiles


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


def _topic_affinities(user_id: str, events: list[CandidateEvent]) -> dict[str, float]:
    """
    For each topic label, compute how often the user participated in events
    with that label. Returns normalised scores.
    """
    topic_counts: dict[str, int] = defaultdict(int)
    total = 0

    for event in events:
        if user_id not in event.participant_ids:
            continue
        if not event.signals:
            continue
        for label in event.signals.topic_labels:
            topic_counts[label] += 1
            total += 1

    if total == 0:
        return {}

    return {topic: round(count / total, 3) for topic, count in topic_counts.items()}


def _event_type_affinities(user_id: str, events: list[CandidateEvent]) -> dict[str, float]:
    """
    Infer which event types the user tends to engage with.
    """
    type_counts: dict[str, float] = defaultdict(float)
    total = 0.0

    for event in events:
        if user_id not in event.participant_ids:
            continue
        if not event.signals:
            continue
        dist = event.signals.event_type_dist
        type_counts["blocker"] += dist.blocker
        type_counts["decision"] += dist.decision
        type_counts["risk"] += dist.risk
        type_counts["status_update"] += dist.status_update
        type_counts["noise"] += dist.noise
        total += 1.0

    if total == 0:
        return {}

    return {t: round(c / total, 3) for t, c in type_counts.items() if c > 0}


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
