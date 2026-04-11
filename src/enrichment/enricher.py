"""
Semantic enrichment stage.

Takes raw CandidateEvents and populates their `.signals` field using
the heuristic signal functions.
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.models import CandidateEvent, SemanticSignals, EventTypeDistribution, SlackWorkspace
from .signals import (
    compute_event_type_scores,
    compute_urgency,
    compute_momentum,
    compute_novelty,
    compute_unresolved,
    compute_cross_functional,
    compute_importance,
    compute_recency,
    compute_title,
    _extract_topic_labels,
)


def enrich_candidate_events(
    events: list[CandidateEvent],
    workspace: SlackWorkspace,
    now: datetime | None = None,
) -> list[CandidateEvent]:
    """
    Enrich all candidate events in-place (returns same list for convenience).
    """
    if now is None:
        now = datetime.now(tz=timezone.utc)

    for event in events:
        event.signals = _enrich_single(event, events, workspace, now)

    return events


def _enrich_single(
    event: CandidateEvent,
    all_events: list[CandidateEvent],
    workspace: SlackWorkspace,
    now: datetime,
) -> SemanticSignals:
    type_scores = compute_event_type_scores(event)

    # Noise check: if noise signal dominates, reduce other signals proportionally
    noise_score = type_scores.get("noise", 0.0)
    noise_suppression = 1.0 - (noise_score * 0.7)  # Up to 70% suppression at full noise

    urgency = compute_urgency(event) * noise_suppression
    momentum = compute_momentum(event)
    novelty = compute_novelty(event, all_events, now)
    unresolved = compute_unresolved(event) * noise_suppression
    importance = compute_importance(event, type_scores) * noise_suppression
    cross_func = compute_cross_functional(event, workspace)
    topic_labels = _extract_topic_labels(event.text_bundle)
    title = compute_title(event, type_scores)

    dominant = max(type_scores, key=type_scores.get)

    # Confidence: how clear-cut is the dominant type?
    sorted_scores = sorted(type_scores.values(), reverse=True)
    top, second = sorted_scores[0], sorted_scores[1] if len(sorted_scores) > 1 else 0.0
    confidence = 0.5 + 0.5 * (top - second)  # Range [0.5, 1.0]
    # If dominant is noise, cap confidence lower
    if dominant == "noise":
        confidence = min(confidence, 0.7)

    return SemanticSignals(
        title=title,
        topic_labels=topic_labels,
        event_type_dist=EventTypeDistribution(**type_scores),
        dominant_event_type=dominant,
        urgency_score=round(urgency, 3),
        momentum_score=round(momentum, 3),
        novelty_score=round(novelty, 3),
        unresolved_score=round(unresolved, 3),
        importance_score=round(importance, 3),
        cross_functional_score=round(cross_func, 3),
        confidence=round(confidence, 3),
    )
