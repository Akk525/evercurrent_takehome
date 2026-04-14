"""
Derived entity schemas — produced by the engine, not loaded from Slack directly.

All scores are floats in [0, 1] unless noted otherwise.
All inferences are probabilistic; nothing here should be treated as ground truth.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Candidate event intermediate objects
# ---------------------------------------------------------------------------

class EventTypeDistribution(BaseModel):
    """
    Soft probability distribution over possible event types.
    Values are not required to sum to 1 — treat each as an independent signal
    rather than a strict categorical distribution.
    """
    blocker: float = 0.0
    decision: float = 0.0
    status_update: float = 0.0
    risk: float = 0.0
    request_for_input: float = 0.0
    noise: float = 0.0  # Social / low-signal chatter


class SemanticSignals(BaseModel):
    """Inferred semantic signals for a candidate event."""
    # Short inferred title (provisional — LLM may later refine)
    title: str
    # Broad topic labels inferred from keywords / channel context
    topic_labels: list[str] = Field(default_factory=list)
    # Soft event type probabilities
    event_type_dist: EventTypeDistribution = Field(default_factory=EventTypeDistribution)
    # Dominant event type (argmax of event_type_dist)
    dominant_event_type: str = "noise"

    # Scalar inferred signals — all in [0, 1]
    urgency_score: float = 0.0        # How time-sensitive does this appear?
    momentum_score: float = 0.0       # Is activity accelerating recently?
    novelty_score: float = 0.0        # Is this a new topic / not seen before?
    unresolved_score: float = 0.0     # Does the thread appear unresolved?
    importance_score: float = 0.0     # Aggregate importance signal
    cross_functional_score: float = 0.0  # Do participants span multiple channels/functions?

    # How confident are we in these inferences overall?
    confidence: float = 0.5

    # Embedding-based similarity scores to topic prototypes (filled by enricher if store available)
    embedding_topic_scores: dict[str, float] = Field(default_factory=dict)
    # Embedding-based novelty (filled by enricher if store available; None = not computed)
    embedding_novelty_score: Optional[float] = None


class CandidateEvent(BaseModel):
    """
    A candidate digest item derived from one Slack thread (MVP: 1 thread = 1 event).

    This is the unit of reasoning for ranking. We never rank raw messages —
    we rank candidate events.
    """
    event_id: str
    thread_id: str
    channel_id: str

    participant_ids: list[str]
    message_ids: list[str]

    started_at: datetime
    last_activity_at: datetime

    # Full text bundle for NLP / embedding (concatenation of all messages in thread)
    text_bundle: str

    # Basic aggregate stats
    message_count: int
    reply_count: int
    unique_participant_count: int
    total_reactions: int

    # Filled in by the enrichment stage
    signals: Optional[SemanticSignals] = None


# ---------------------------------------------------------------------------
# User context profile
# ---------------------------------------------------------------------------

class UserContextProfile(BaseModel):
    """
    Behavioural profile inferred entirely from Slack activity.
    No hardcoded roles or org-chart assumptions.
    """
    user_id: str

    # Channels the user has posted in, weighted by recency + frequency
    active_channel_ids: list[str] = Field(default_factory=list)

    # Topic labels the user has engaged with (from candidate event signals)
    topic_affinities: dict[str, float] = Field(default_factory=dict)

    # Event types the user tends to engage with
    event_type_affinities: dict[str, float] = Field(default_factory=dict)

    # Users the user frequently collaborates with
    frequent_collaborators: list[str] = Field(default_factory=list)

    # Thread IDs the user has participated in (recent activity window)
    recent_thread_ids: list[str] = Field(default_factory=list)

    # Normalised activity level (0 = inactive, 1 = most active user in workspace)
    activity_level: float = 0.0

    # Interaction-weighted affinity per thread (thread_id → weight)
    # Values are normalised so the highest weight in the workspace = 1.0
    interaction_weights: dict[str, float] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Ranking and digest output
# ---------------------------------------------------------------------------

class RankingFeatures(BaseModel):
    """Explainable per-feature scores used to compute final relevance score."""
    user_affinity: float           # How closely does this event match the user's profile?
    importance: float              # Event-level importance signal
    urgency: float                 # Event-level urgency signal
    momentum: float                # Event-level momentum signal
    novelty: float                 # Event-level novelty signal
    recency: float                 # How recent is the last activity?
    embedding_affinity: float = 0.0  # Embedding cosine similarity to user interest profile

    # Weights used (stored for traceability — not just the final number)
    weights: dict[str, float]

    # Final weighted score
    final_score: float


class ExcludedDigestItem(BaseModel):
    """
    A candidate event that was considered but did not make the top-k cut.
    Stored for explainability and debug inspection.
    """
    event_id: str
    title: str
    score: float
    top_exclusion_reason: str  # Human-readable explanation of why this was not selected


class RankedDigestItem(BaseModel):
    """A single item in a user's daily digest, with full traceability."""
    event_id: str
    title: str
    summary: Optional[str] = None          # Filled by LLM or fallback
    why_shown: Optional[str] = None        # Filled by LLM or fallback
    signal_level: str                      # "high" | "medium" | "low"
    event_type: str
    confidence: float
    score: float
    reason_features: RankingFeatures
    source_thread_ids: list[str]
    source_message_ids: list[str]


class DailyDigest(BaseModel):
    """Final per-user digest payload."""
    user_id: str
    date: str                              # ISO date string "YYYY-MM-DD"
    headline: str
    items: list[RankedDigestItem]
    # Metadata about the run
    generated_at: datetime
    total_candidates_considered: int
    llm_used: bool = False
    # Events considered but not selected — populated when include_excluded=True
    excluded_items: list[ExcludedDigestItem] = Field(default_factory=list)


class SharedEventSummary(BaseModel):
    """
    Event-level summary shared across all users who see this event.
    Computed once per event, reused across users to avoid redundant summarization.
    """
    event_id: str
    title: str
    summary: str        # Shared situation + impact + resolution text
    event_type: str
    unresolved: bool    # True if unresolved_score > 0.5
    confidence: float
