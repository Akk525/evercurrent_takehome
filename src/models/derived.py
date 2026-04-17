"""
Derived entity schemas — produced by the engine, not loaded from Slack directly.

All scores are floats in [0, 1] unless noted otherwise.
All inferences are probabilistic; nothing here should be treated as ground truth.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    # All signal model imports are TYPE_CHECKING-only to avoid circular imports.
    # src.enrichment.__init__ imports enricher.py which imports src.models — circular.
    # At runtime these fields remain Optional[Any]; TYPE_CHECKING imports serve IDEs/mypy.
    from src.issue_memory.matcher import IssueMemorySignals
    from src.enrichment.ownership_models import OwnershipSignals
    from src.enrichment.drift_models import DriftSignals
    from src.impact.graph_models import GraphSignals


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

    # --- V2 additions ---

    # Hybrid event type scores: blended heuristic + semantic similarity
    # Keys match EventTypeDistribution fields. Replaces heuristic-only type_scores
    # for classification but heuristic scores are still stored for traceability.
    hybrid_event_type_scores: dict[str, float] = Field(default_factory=dict)

    # Structured entities extracted from the text bundle
    # Keys: "parts", "revisions", "builds", "suppliers", "subsystems", "deadlines"
    extracted_entities: dict[str, list[str]] = Field(default_factory=dict)

    # Detected state transition (heuristic), e.g. "unresolved → decision made"
    # None if no transition detected.
    state_change_hint: Optional[str] = None

    # Per-type classification confidence breakdown
    # Maps event type name → confidence in that classification
    type_confidence: dict[str, float] = Field(default_factory=dict)


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

    # --- V2 additions (filled by issue_linking stage) ---

    # V3: Issue memory signals — ephemeral per run, set by matcher, not serialized.
    # Runtime: Optional[Any] avoids Pydantic forward-ref resolution at load time.
    # Static analysis: TYPE_CHECKING import above provides IssueMemorySignals type hint.
    issue_memory_signals: Optional[Any] = Field(default=None, exclude=True)

    # ID of the issue cluster this event belongs to (None = no cluster assigned)
    issue_cluster_id: Optional[str] = None

    # Other event IDs in the same issue cluster
    related_event_ids: list[str] = Field(default_factory=list)

    # Issue status: "new" | "ongoing" | "resurfacing"
    issue_status: str = "new"

    # --- V4: Extended inference signals (populated after enrichment, before ranking) ---
    # Stage order: issue_linking → issue_memory → ownership → drift → graph → ranking
    # All excluded from API serialization.
    # Runtime: Optional[Any] — avoids circular import through src.enrichment and src.impact.
    # Static analysis: TYPE_CHECKING imports above provide GraphSignals/OwnershipSignals/DriftSignals.
    graph_signals: Optional[Any] = Field(default=None, exclude=True)
    ownership_signals: Optional[Any] = Field(default=None, exclude=True)
    drift_signals: Optional[Any] = Field(default=None, exclude=True)


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

    # --- V2 additions ---

    # Semantic topic affinities computed from embeddings of events the user engaged with.
    # Keys are topic prototype names (same as EmbeddingStore.topic_vecs).
    # Complements keyword-based topic_affinities with embedding-derived signal.
    semantic_topic_affinities: dict[str, float] = Field(default_factory=dict)


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

    # --- V2 grouped sub-scores for better explainability ---
    # Personal relevance: how much does this event concern this specific user?
    personal_relevance: float = 0.0   # weighted(user_affinity + embedding_affinity)
    # Global importance: how objectively critical is this event?
    global_importance: float = 0.0    # weighted(importance + urgency)
    # Freshness: how active/recent/new is this?
    freshness: float = 0.0            # weighted(momentum + novelty + recency)

    # Issue cluster context (if available)
    issue_cluster_id: Optional[str] = None
    cluster_related_count: int = 0    # How many related events in this cluster

    # --- V3: Issue memory signals (if available) ---
    # Populated after issue memory matching; 0.0 / "" when no memory record found
    issue_persistence_score: float = 0.0   # [0,1] — how long-running / recurrent
    issue_escalation_score: float = 0.0    # [0,1] — how severe / escalated
    issue_memory_label: str = ""           # "Ongoing for 2 days", "Resurfaced", etc.

    # --- V4: Graph-derived ranking boost ---
    graph_impact_boost: float = 0.0        # From downstream impact count in graph
    graph_centrality_score: float = 0.0    # Degree centrality [0,1]


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
    # V3: grounded one-sentence impact statement ("why this matters to the project")
    impact_statement: Optional[str] = None
    signal_level: str                      # "high" | "medium" | "low"
    event_type: str
    confidence: float
    score: float
    reason_features: RankingFeatures
    source_thread_ids: list[str]
    source_message_ids: list[str]

    # --- V4: Extended context for UI and downstream (serialized in API payloads) ---
    # Runtime type is Any to avoid circular import; IDEs/mypy see typed hints via TYPE_CHECKING.
    ownership_signals: Optional[Any] = Field(default=None)
    drift_signals: Optional[Any] = Field(default=None)


class DigestSections(BaseModel):
    """
    Structured bucketing of digest items into named sections.

    Not all sections are always populated — depends on available signals.
    The flat `items` list on DailyDigest is the canonical output;
    sections are an optional layer for richer UI or downstream consumers.
    """
    top_for_you: list[str] = Field(default_factory=list)        # event_ids
    also_worth_attention: list[str] = Field(default_factory=list)
    what_changed: list[str] = Field(default_factory=list)        # state-change items
    still_unresolved: list[str] = Field(default_factory=list)    # high unresolved_score


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
    # Optional structured sections (V2)
    sections: Optional[DigestSections] = None


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


