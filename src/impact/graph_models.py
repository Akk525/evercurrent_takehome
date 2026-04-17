"""
Data models for the dependency & impact graph.

These models are intentionally thin — they wrap signals already present on
CandidateEvents into a graph representation without recomputing enrichment.

All confidence scores are in [0, 1]. All inferences are probabilistic.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class GraphEdge(BaseModel):
    source_event_id: str
    target_event_id: str
    relation_type: str   # "blocks" | "depends_on" | "related_to" | "impacts"
    confidence: float    # [0, 1]
    explanation: str     # Human-readable reason for this edge


class GraphNode(BaseModel):
    event_id: str
    title: str
    signal_level: str           # "high" | "medium" | "low"
    dominant_event_type: str
    issue_cluster_id: Optional[str]
    upstream_count: int         # How many events depend on this
    downstream_count: int       # How many events this blocks
    centrality_score: float     # [0, 1] normalized degree centrality


class IssueGraph(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]


class GraphSignals(BaseModel):
    """Graph-derived signals for a single CandidateEvent."""
    downstream_impact_count: int = 0       # How many events this blocks
    upstream_dependency_count: int = 0     # How many events this depends on
    blocks_event_ids: list[str] = Field(default_factory=list)
    depends_on_event_ids: list[str] = Field(default_factory=list)
    graph_centrality_score: float = 0.0    # [0, 1]
    graph_impact_boost: float = 0.0        # Additional ranking boost from graph position [0, 1]
