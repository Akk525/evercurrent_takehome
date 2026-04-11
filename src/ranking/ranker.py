"""
Per-user relevance ranking for candidate events.

Scoring function:
    score = w1 * user_affinity
          + w2 * importance
          + w3 * urgency
          + w4 * momentum
          + w5 * novelty
          + w6 * recency

Everything is feature-driven and explainable.
Weights are fully tunable.
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.models import (
    CandidateEvent,
    UserContextProfile,
    RankedDigestItem,
    RankingFeatures,
)
from src.enrichment.signals import compute_recency


# Default weights — tune these without touching scoring logic
DEFAULT_WEIGHTS: dict[str, float] = {
    "user_affinity": 0.30,
    "importance": 0.25,
    "urgency": 0.20,
    "momentum": 0.10,
    "novelty": 0.10,
    "recency": 0.05,
}


def rank_events_for_user(
    events: list[CandidateEvent],
    profile: UserContextProfile,
    top_k: int = 5,
    weights: dict[str, float] | None = None,
    now: datetime | None = None,
) -> list[RankedDigestItem]:
    """
    Score all enriched events for a given user profile and return top-k ranked items.

    Events without signals (unenriched) are skipped.
    """
    if weights is None:
        weights = DEFAULT_WEIGHTS
    if now is None:
        now = datetime.now(tz=timezone.utc)

    scored: list[tuple[float, RankedDigestItem]] = []

    for event in events:
        if event.signals is None:
            continue

        features = _compute_features(event, profile, weights, now)
        item = _build_digest_item(event, features)
        scored.append((features.final_score, item))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in scored[:top_k]]


def _compute_features(
    event: CandidateEvent,
    profile: UserContextProfile,
    weights: dict[str, float],
    now: datetime,
) -> RankingFeatures:
    signals = event.signals

    user_affinity = _compute_user_affinity(event, profile)
    importance = signals.importance_score
    urgency = signals.urgency_score
    momentum = signals.momentum_score
    novelty = signals.novelty_score
    recency = compute_recency(event, now)

    final_score = (
        weights["user_affinity"] * user_affinity
        + weights["importance"] * importance
        + weights["urgency"] * urgency
        + weights["momentum"] * momentum
        + weights["novelty"] * novelty
        + weights["recency"] * recency
    )

    return RankingFeatures(
        user_affinity=round(user_affinity, 3),
        importance=round(importance, 3),
        urgency=round(urgency, 3),
        momentum=round(momentum, 3),
        novelty=round(novelty, 3),
        recency=round(recency, 3),
        weights=weights,
        final_score=round(final_score, 3),
    )


def _compute_user_affinity(
    event: CandidateEvent,
    profile: UserContextProfile,
) -> float:
    """
    User affinity score for a (user, event) pair.

    Components:
    1. Direct participation: user was in this thread (+strong boost)
    2. Channel affinity: user is active in this channel
    3. Topic affinity: event topics overlap with user's topic interests
    4. Collaborator affinity: user frequently works with event participants
    5. Mention bonus: user was explicitly mentioned
    """
    score = 0.0

    # 1. Direct participation — strongest signal
    if profile.user_id in event.participant_ids:
        score += 0.4

    # 2. Channel affinity
    if event.channel_id in profile.active_channel_ids:
        channel_rank = profile.active_channel_ids.index(event.channel_id)
        channel_score = max(0.0, 0.2 - channel_rank * 0.04)  # Up to 0.2, decays by rank
        score += channel_score

    # 3. Topic affinity
    if event.signals and profile.topic_affinities:
        topic_overlap = sum(
            profile.topic_affinities.get(label, 0.0)
            for label in event.signals.topic_labels
        )
        score += min(topic_overlap * 0.3, 0.3)  # Cap at 0.3

    # 4. Collaborator affinity
    event_participants = set(event.participant_ids)
    collaborator_set = set(profile.frequent_collaborators)
    overlap_count = len(event_participants & collaborator_set)
    score += min(overlap_count * 0.05, 0.15)  # Up to 0.15

    return min(score, 1.0)


def _signal_level(score: float) -> str:
    if score >= 0.65:
        return "high"
    elif score >= 0.40:
        return "medium"
    return "low"


def _build_digest_item(
    event: CandidateEvent,
    features: RankingFeatures,
) -> RankedDigestItem:
    signals = event.signals

    return RankedDigestItem(
        event_id=event.event_id,
        title=signals.title,
        summary=None,   # Filled by LLM or fallback summarizer
        why_shown=None, # Filled by summarizer
        signal_level=_signal_level(features.final_score),
        event_type=signals.dominant_event_type,
        confidence=signals.confidence,
        score=features.final_score,
        reason_features=features,
        source_thread_ids=[event.thread_id],
        source_message_ids=event.message_ids,
    )
