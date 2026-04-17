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
    compute_state_change_hint,
    _extract_topic_labels,
)
from .entities import extract_entities

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

    # NOTE: Ownership inference and drift detection are NOT run here.
    # They depend on issue_memory_signals (set by match_and_update_issues, which runs
    # after issue linking). Both stages must be called explicitly in the pipeline
    # orchestration layer (api/server.py _run_pipeline) AFTER issue memory matching.

    return events


def _build_embedding_store(events: list[CandidateEvent]) -> EmbeddingStore:
    """
    Fit an embedding store on the event corpus.

    Provider is selected via DIGEST_EMBEDDING_PROVIDER env var (default: tfidf).
    See src.embeddings.provider.get_embedding_provider for valid values.
    """
    from src.embeddings import get_embedding_provider
    provider = get_embedding_provider()
    store = EmbeddingStore(provider=provider)
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
    # --- Structural enrichment (cheap, no embeddings) ---
    type_scores = compute_event_type_scores(event)

    # Noise check: if noise signal dominates, suppress technical signals proportionally
    noise_score = type_scores.get("noise", 0.0)
    noise_suppression = 1.0 - (noise_score * 0.7)  # Up to 70% suppression at full noise

    urgency = compute_urgency(event) * noise_suppression
    if thread_messages:
        momentum = compute_momentum_enhanced(event, thread_messages)
    else:
        momentum = compute_momentum(event)
    unresolved = compute_unresolved(event) * noise_suppression
    cross_func = compute_cross_functional(event, workspace)
    topic_labels = _extract_topic_labels(event.text_bundle)

    # --- Semantic enrichment (embeddings + entity extraction) ---
    embedding_topic_scores = embedding_store.topic_similarity_scores(event.event_id)
    event_type_sim_scores = embedding_store.event_type_similarity_scores(event.event_id)
    other_event_ids = [e.event_id for e in all_events if e.event_id != event.event_id]
    embedding_novelty = embedding_store.novelty_score(event.event_id, other_event_ids)

    # Hybrid event type classification: blend heuristic (alpha) + semantic (beta)
    # alpha=0.6, beta=0.4 — heuristic precision anchors, embedding adds recall
    alpha, beta = 0.6, 0.4
    hybrid_type_scores = _blend_type_scores(type_scores, event_type_sim_scores, alpha, beta)

    # Recompute importance and dominance using hybrid scores
    importance = compute_importance(event, hybrid_type_scores) * noise_suppression
    dominant = max(hybrid_type_scores, key=hybrid_type_scores.get)

    # Type confidence: gap between top-2 hybrid scores per type
    type_confidence = _compute_type_confidence(hybrid_type_scores)

    # Blend keyword novelty with embedding novelty (50/50)
    keyword_novelty = compute_novelty(event, all_events, now)
    novelty = round(0.5 * keyword_novelty + 0.5 * embedding_novelty, 3) * noise_suppression

    # Augment topic labels with high-similarity embedding topics
    if embedding_topic_scores:
        for topic, sim in embedding_topic_scores.items():
            if sim > 0.15 and topic not in topic_labels:
                topic_labels.append(topic)

    # Entity extraction
    entities = extract_entities(event.text_bundle)
    extracted_entities = {k: v for k, v in entities.to_dict().items() if v}

    # State-change detection
    state_change_hint = compute_state_change_hint(event)

    # Title uses hybrid dominant type
    title = compute_title(event, hybrid_type_scores)

    # Confidence uses hybrid scores and embeddings
    confidence = compute_confidence(event, hybrid_type_scores, embedding_topic_scores)

    return SemanticSignals(
        title=title,
        topic_labels=topic_labels,
        event_type_dist=EventTypeDistribution(**{k: round(v, 3) for k, v in hybrid_type_scores.items()}),
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
        hybrid_event_type_scores={k: round(v, 3) for k, v in hybrid_type_scores.items()},
        extracted_entities=extracted_entities,
        state_change_hint=state_change_hint,
        type_confidence={k: round(v, 3) for k, v in type_confidence.items()},
    )


def _blend_type_scores(
    heuristic: dict[str, float],
    semantic: dict[str, float],
    alpha: float,
    beta: float,
) -> dict[str, float]:
    """
    Blend heuristic and semantic event type scores.

    alpha * heuristic + beta * semantic, normalised so values stay in [0, 1].
    If semantic scores are empty (no embedding store), falls back to heuristic only.
    """
    if not semantic:
        return heuristic.copy()

    types = set(heuristic) | set(semantic)
    blended = {}
    for t in types:
        h = heuristic.get(t, 0.0)
        s = semantic.get(t, 0.0)
        blended[t] = min(alpha * h + beta * s, 1.0)

    return blended


def _compute_type_confidence(hybrid_scores: dict[str, float]) -> dict[str, float]:
    """
    Per-type confidence: how clearly does the text classify as each type?

    For each type, confidence = score / (score + second_highest_competitor).
    A score of 0.0 gets confidence 0.0.
    """
    confidence: dict[str, float] = {}
    for t, score in hybrid_scores.items():
        if score <= 0:
            confidence[t] = 0.0
            continue
        others = [v for k, v in hybrid_scores.items() if k != t]
        second = max(others, default=0.0)
        total = score + second
        confidence[t] = round(score / total, 3) if total > 0 else 0.0
    return confidence
