"""
Semantic enrichment stage.

Takes raw CandidateEvents and populates their `.signals` field using
heuristic signal functions plus optional embedding-based signals.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from src.models import CandidateEvent, SemanticSignals, EventTypeDistribution, SlackWorkspace
from src.embeddings import EmbeddingStore
from .signals import (
    compute_event_type_scores,
    compute_urgency,
    compute_momentum,
    compute_momentum_enhanced,
    compute_novelty,
    compute_unresolved,
    compute_cross_functional,
    compute_importance,
    compute_confidence,
    compute_title,
    _extract_topic_labels,
)

if TYPE_CHECKING:
    from src.cache import ProcessingState


def enrich_candidate_events(
    events: list[CandidateEvent],
    workspace: SlackWorkspace,
    now: datetime | None = None,
    embedding_store: Optional[EmbeddingStore] = None,
    processing_state: Optional["ProcessingState"] = None,
) -> list[CandidateEvent]:
    """
    Enrich all candidate events in-place (returns same list for convenience).

    If embedding_store is provided:
    - topic similarity scores are computed per event
    - novelty uses embedding similarity instead of keyword overlap
    """
    if now is None:
        now = datetime.now(tz=timezone.utc)

    # Build embedding store if not provided
    if embedding_store is None:
        embedding_store = _build_embedding_store(events)

    # Index messages by thread for enhanced momentum computation
    messages_by_thread: dict[str, list] = defaultdict(list)
    for msg in workspace.messages:
        messages_by_thread[msg.thread_id].append(msg)

    for event in events:
        thread_msgs = messages_by_thread.get(event.thread_id, [])

        # Incremental processing: skip events whose content hasn't changed
        if processing_state is not None and not processing_state.is_dirty(event):
            print(f"[cache] Skipping clean event: {event.event_id}")
            continue  # Reuse existing event.signals from previous run

        event.signals = _enrich_single(
            event, events, workspace, now, thread_msgs, embedding_store
        )

        if processing_state is not None:
            processing_state.mark_clean(event)

    return events


def _build_embedding_store(events: list[CandidateEvent]) -> EmbeddingStore:
    """Fit a fresh TF-IDF embedding store on the event corpus."""
    store = EmbeddingStore()
    texts = [e.text_bundle for e in events]
    keys = [e.event_id for e in events]
    store.fit_and_embed(texts, keys)
    return store


def _enrich_single(
    event: CandidateEvent,
    all_events: list[CandidateEvent],
    workspace: SlackWorkspace,
    now: datetime,
    thread_messages: list,
    embedding_store: EmbeddingStore,
) -> SemanticSignals:
    type_scores = compute_event_type_scores(event)

    # Noise check: if noise signal dominates, suppress technical signals proportionally
    noise_score = type_scores.get("noise", 0.0)
    noise_suppression = 1.0 - (noise_score * 0.7)  # Up to 70% suppression at full noise

    urgency = compute_urgency(event) * noise_suppression
    # Use enhanced momentum if we have per-message data; fall back to basic
    if thread_messages:
        momentum = compute_momentum_enhanced(event, thread_messages)
    else:
        momentum = compute_momentum(event)
    unresolved = compute_unresolved(event) * noise_suppression
    importance = compute_importance(event, type_scores) * noise_suppression
    cross_func = compute_cross_functional(event, workspace)
    topic_labels = _extract_topic_labels(event.text_bundle)
    title = compute_title(event, type_scores)

    # Embedding-based signals
    embedding_topic_scores = embedding_store.topic_similarity_scores(event.event_id)
    other_event_ids = [e.event_id for e in all_events if e.event_id != event.event_id]
    embedding_novelty = embedding_store.novelty_score(event.event_id, other_event_ids)

    # Blend keyword novelty with embedding novelty (50/50)
    keyword_novelty = compute_novelty(event, all_events, now)
    novelty = round(0.5 * keyword_novelty + 0.5 * embedding_novelty, 3) * noise_suppression

    # Augment topic labels with high-similarity embedding topics
    # (adds topics detected by embedding but missed by keyword matching)
    if embedding_topic_scores:
        for topic, sim in embedding_topic_scores.items():
            if sim > 0.15 and topic not in topic_labels:  # Threshold: avoid noise
                topic_labels.append(topic)

    dominant = max(type_scores, key=type_scores.get)

    # Confidence: composite of type score concentration, signal richness,
    # and keyword-embedding agreement. See signals.compute_confidence() for details.
    confidence = compute_confidence(event, type_scores, embedding_topic_scores)

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
        embedding_topic_scores=embedding_topic_scores,
        embedding_novelty_score=round(embedding_novelty, 3),
    )
