"""
Per-user relevance ranking for candidate events.

Scoring function:
    score = w1 * user_affinity
          + w2 * importance
          + w3 * urgency
          + w4 * momentum
          + w5 * novelty
          + w6 * recency
          + w7 * embedding_affinity  (new)

All features and weights are exposed for traceability.
Supports per-user weight overrides via RankingConfig.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from src.models import (
    CandidateEvent,
    UserContextProfile,
    RankedDigestItem,
    RankingFeatures,
    ExcludedDigestItem,
)
from src.enrichment.signals import compute_recency
from src.embeddings import EmbeddingStore
from .config import RankingConfig, DEFAULT_WEIGHTS


def rank_events_for_user(
    events: list[CandidateEvent],
    profile: UserContextProfile,
    top_k: int = 5,
    weights: dict[str, float] | None = None,  # Kept for backward compat; superseded by config
    now: datetime | None = None,
    config: RankingConfig | None = None,
    embedding_store: EmbeddingStore | None = None,
    include_excluded: bool = False,
    pruning_config=None,  # Optional[PruningConfig] — imported lazily to avoid circular imports
) -> tuple[list[RankedDigestItem], list[ExcludedDigestItem]]:
    """
    Score all enriched events for a given user profile and return top-k ranked items.

    Returns (selected_items, excluded_items).
    excluded_items is populated only when include_excluded=True.

    Backward compat: existing callers that don't unpack the tuple will still work
    if they only access the first element.
    """
    if config is not None:
        effective_weights = config.weights_for_user(profile.user_id)
        effective_top_k = config.top_k
    else:
        effective_weights = weights if weights is not None else DEFAULT_WEIGHTS
        effective_top_k = top_k

    if now is None:
        now = datetime.now(tz=timezone.utc)

    # Optional pre-ranking candidate pruning
    events_to_score = events
    pruned_excluded: list[ExcludedDigestItem] = []

    if pruning_config is not None:
        from .pruner import prune_candidates
        events_to_score, pruning_stats = prune_candidates(events, profile, pruning_config)
        print(f"[pruning] user={profile.user_id}: {pruning_stats.total} → {pruning_stats.kept} candidates")

        if include_excluded:
            for pruned_id in pruning_stats.pruned_ids:
                pruned_event = next((e for e in events if e.event_id == pruned_id), None)
                title = (
                    pruned_event.signals.title
                    if pruned_event and pruned_event.signals
                    else pruned_id
                )
                pruned_excluded.append(ExcludedDigestItem(
                    event_id=pruned_id,
                    title=title,
                    score=0.0,
                    top_exclusion_reason="pruned before scoring — below relevance threshold",
                ))

    scored: list[tuple[float, RankedDigestItem]] = []

    for event in events_to_score:
        if event.signals is None:
            continue

        features = _compute_features(
            event, profile, effective_weights, now,
            embedding_store,
        )
        item = _build_digest_item(event, features)
        scored.append((features.final_score, item))

    scored.sort(key=lambda x: x[0], reverse=True)

    selected = [item for _, item in scored[:effective_top_k]]
    excluded: list[ExcludedDigestItem] = []

    if include_excluded:
        for _, item in scored[effective_top_k:]:
            excluded.append(ExcludedDigestItem(
                event_id=item.event_id,
                title=item.title,
                score=item.score,
                top_exclusion_reason=_exclusion_reason(item),
            ))
        # Append any events pruned before scoring
        excluded.extend(pruned_excluded)

    return selected, excluded


def _compute_features(
    event: CandidateEvent,
    profile: UserContextProfile,
    weights: dict[str, float],
    now: datetime,
    embedding_store: EmbeddingStore | None,
) -> RankingFeatures:
    signals = event.signals

    user_affinity = _compute_user_affinity(event, profile)
    importance = signals.importance_score
    urgency = signals.urgency_score
    momentum = signals.momentum_score
    novelty = signals.novelty_score
    recency = compute_recency(event, now)

    # Embedding affinity: topic-prototype weighted affinity.
    # Primary path uses event.signals.embedding_topic_scores (pre-computed during enrichment)
    # weighted by the user's topic affinities — no store needed.
    # Fallback path (when topic scores are absent) uses embedding_store for a query embed.
    embedding_affinity = _compute_embedding_affinity(event, profile, embedding_store)

    final_score = (
        weights.get("user_affinity", 0) * user_affinity
        + weights.get("importance", 0) * importance
        + weights.get("urgency", 0) * urgency
        + weights.get("momentum", 0) * momentum
        + weights.get("novelty", 0) * novelty
        + weights.get("recency", 0) * recency
        + weights.get("embedding_affinity", 0) * embedding_affinity
    )

    return RankingFeatures(
        user_affinity=round(user_affinity, 3),
        importance=round(importance, 3),
        urgency=round(urgency, 3),
        momentum=round(momentum, 3),
        novelty=round(novelty, 3),
        recency=round(recency, 3),
        embedding_affinity=round(embedding_affinity, 3),
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
    1. Direct participation (with interaction-weight boost)
    2. Channel affinity
    3. Topic affinity (from weighted profile)
    4. Collaborator affinity
    """
    score = 0.0

    # 1. Direct participation — boosted by interaction weight if available
    if profile.user_id in event.participant_ids:
        interaction_w = profile.interaction_weights.get(event.thread_id, 0.0)
        # Flat boost of 0.3, plus up to 0.15 extra from interaction strength
        score += 0.3 + min(interaction_w * 0.15, 0.15)

    # 2. Channel affinity (decays by channel rank)
    if event.channel_id in profile.active_channel_ids:
        channel_rank = profile.active_channel_ids.index(event.channel_id)
        channel_score = max(0.0, 0.2 - channel_rank * 0.04)
        score += channel_score

    # 3. Topic affinity (from interaction-weighted profile)
    if event.signals and profile.topic_affinities:
        topic_overlap = sum(
            profile.topic_affinities.get(label, 0.0)
            for label in event.signals.topic_labels
        )
        score += min(topic_overlap * 0.3, 0.3)

    # 4. Collaborator affinity
    event_participants = set(event.participant_ids)
    collaborator_set = set(profile.frequent_collaborators)
    overlap_count = len(event_participants & collaborator_set)
    score += min(overlap_count * 0.05, 0.15)

    return min(score, 1.0)


def _compute_embedding_affinity(
    event: CandidateEvent,
    profile: UserContextProfile,
    embedding_store: EmbeddingStore | None,
) -> float:
    """
    Compute embedding-based affinity between an event and a user's interests.

    Primary path: use pre-computed embedding_topic_scores (stored on the event
    during enrichment) weighted by the user's topic affinities. No store required.

    Fallback path: if the event has no embedding_topic_scores (edge case),
    use embedding_store.user_profile_affinity() with a synthesised interest text.

    Returns a value in [0, 1].
    """
    if not profile.topic_affinities:
        return 0.0

    event_topic_scores = event.signals.embedding_topic_scores if event.signals else {}
    if not event_topic_scores:
        # Fallback: requires embedding_store
        if embedding_store is None or not embedding_store.has(event.event_id):
            return 0.0
        user_interest_text = " ".join(
            t for t, _ in sorted(profile.topic_affinities.items(), key=lambda x: -x[1])[:5]
        )
        if user_interest_text:
            return embedding_store.user_profile_affinity(event.event_id, user_interest_text)
        return 0.0

    # Weight each event topic score by the user's affinity for that topic.
    # Normalise user affinities so they sum to 1 (prevents scale distortion).
    total_affinity = sum(profile.topic_affinities.values())
    if total_affinity <= 0:
        return 0.0

    weighted_sum = 0.0
    weight_sum = 0.0
    for topic, user_weight in profile.topic_affinities.items():
        event_score = event_topic_scores.get(topic, 0.0)
        norm_weight = user_weight / total_affinity
        weighted_sum += norm_weight * event_score
        weight_sum += norm_weight

    if weight_sum <= 0:
        return 0.0

    # Scale up: cosine similarity against short prototype texts tends to be low.
    # A raw score of 0.15 on the user's primary topic is meaningful — scale so
    # that 0.15 maps to ~0.5 affinity (linear scaling with ceiling at 1.0).
    raw = weighted_sum / weight_sum
    return round(min(raw * 3.0, 1.0), 3)


def _build_user_interest_text(profile: UserContextProfile) -> str:
    """
    Kept for backward compatibility. No longer used for embedding affinity
    (replaced by _compute_embedding_affinity), but may be useful for debugging.
    """
    top_topics = sorted(
        profile.topic_affinities.items(), key=lambda x: x[1], reverse=True
    )[:5]
    if not top_topics:
        return ""
    return " ".join(topic for topic, _ in top_topics)


def _signal_level(score: float) -> str:
    if score >= 0.65:
        return "high"
    elif score >= 0.40:
        return "medium"
    return "low"


def _exclusion_reason(item: RankedDigestItem) -> str:
    """
    Identify the primary reason this item didn't make the top-k cut.
    Looks for the feature with the lowest weighted contribution.
    """
    f = item.reason_features
    w = f.weights

    feature_contributions = {
        "user_affinity": w.get("user_affinity", 0) * f.user_affinity,
        "importance": w.get("importance", 0) * f.importance,
        "urgency": w.get("urgency", 0) * f.urgency,
        "momentum": w.get("momentum", 0) * f.momentum,
        "novelty": w.get("novelty", 0) * f.novelty,
        "recency": w.get("recency", 0) * f.recency,
        "embedding_affinity": w.get("embedding_affinity", 0) * f.embedding_affinity,
    }

    # The primary exclusion reason is the feature with the lowest contribution
    # relative to its weight (i.e., it pulled the score down the most)
    weakest = min(feature_contributions, key=feature_contributions.get)

    reason_phrases = {
        "user_affinity": f"low user affinity ({f.user_affinity:.2f}) — event not closely tied to user's activity",
        "importance": f"low importance ({f.importance:.2f}) — event lacks strong blocker/risk/decision signals",
        "urgency": f"low urgency ({f.urgency:.2f}) — no strong time-pressure indicators",
        "momentum": f"low momentum ({f.momentum:.2f}) — thread has limited recent activity",
        "novelty": f"low novelty ({f.novelty:.2f}) — topic already seen in other ranked events",
        "recency": f"low recency ({f.recency:.2f}) — last activity was some time ago",
        "embedding_affinity": f"low semantic affinity ({f.embedding_affinity:.2f}) — event text dissimilar to user's interest profile",
    }

    return f"score={item.score:.3f}; primary drag: {reason_phrases.get(weakest, weakest)}"


def _build_digest_item(
    event: CandidateEvent,
    features: RankingFeatures,
) -> RankedDigestItem:
    signals = event.signals

    return RankedDigestItem(
        event_id=event.event_id,
        title=signals.title,
        summary=None,    # Filled by LLM or fallback summarizer
        why_shown=None,  # Filled by summarizer
        signal_level=_signal_level(features.final_score),
        event_type=signals.dominant_event_type,
        confidence=signals.confidence,
        score=features.final_score,
        reason_features=features,
        source_thread_ids=[event.thread_id],
        source_message_ids=event.message_ids,
    )
