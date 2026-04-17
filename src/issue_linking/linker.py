"""
Issue linking: group related CandidateEvents into issue clusters.

A cluster is a set of events that appear to describe the same underlying
engineering issue across one or more threads. This is intentionally lightweight:
no graph database, no ML clustering library. Union-find on pairwise similarity.

Clustering signals (each independent, combined via OR with thresholds):
    1. Entity overlap    — shared part numbers, revisions, suppliers, subsystems
    2. Semantic overlap  — cosine similarity of event embeddings > threshold
    3. Participant overlap — same core participants across threads
    4. Topic overlap     — same dominant topic labels

Issue status assignment:
    Inferred from relative timestamps within the mock data window.
    A cluster with only one event is "new".
    A cluster where the most recent event is >24h after the first is "ongoing".
    "resurfacing" requires: cluster events are separated by a quiet gap (>12h gap
    between consecutive events, followed by renewed activity).

Design:
    - Operates on enriched CandidateEvents (signals + extracted_entities required)
    - Mutates events in-place: sets issue_cluster_id, related_event_ids, issue_status
    - Returns dict[cluster_id, IssueCluster] for downstream use
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ENTITY_OVERLAP_THRESHOLD = 2         # Min shared high-value entities to consider linking

# Semantic similarity threshold varies by embedding provider:
#   - TF-IDF: max pairwise sim is ~0.26 on diverse corpora; 0.40 is intentionally cautious
#   - sentence-transformers: sims range ~0.3–0.8; use a higher absolute threshold
# Configurable via DIGEST_SEMANTIC_SIM_THRESHOLD env var.
def _get_semantic_sim_threshold() -> float:
    import os
    val = os.environ.get("DIGEST_SEMANTIC_SIM_THRESHOLD")
    if val is not None:
        return float(val)
    provider = os.environ.get("DIGEST_EMBEDDING_PROVIDER", "tfidf")
    if provider in ("sentence-transformers", "sentence_transformers", "st"):
        return 0.55
    return 0.40

SEMANTIC_SIM_THRESHOLD = _get_semantic_sim_threshold()
# NOTE: Participant-overlap linking is disabled for small teams where almost all
# threads share 2+ participants. Relies on entity + semantic signals only.


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class IssueCluster:
    cluster_id: str
    event_ids: list[str] = field(default_factory=list)
    dominant_topic: str = ""
    dominant_entity_type: str = ""
    representative_event_id: str = ""  # Highest-scoring event in cluster
    issue_status: str = "new"          # "new" | "ongoing" | "resurfacing"

    def size(self) -> int:
        return len(self.event_ids)


# ---------------------------------------------------------------------------
# Union-Find (path-compressed)
# ---------------------------------------------------------------------------

class _UnionFind:
    def __init__(self, ids: list[str]) -> None:
        self._parent = {i: i for i in ids}

    def find(self, x: str) -> str:
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def union(self, x: str, y: str) -> None:
        rx, ry = self.find(x), self.find(y)
        if rx != ry:
            self._parent[ry] = rx

    def groups(self) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for x in self._parent:
            root = self.find(x)
            result.setdefault(root, []).append(x)
        return result


# ---------------------------------------------------------------------------
# Core linking logic
# ---------------------------------------------------------------------------

def build_issue_clusters(
    events: list,  # list[CandidateEvent] — typed loosely to avoid circular import
    embedding_store=None,  # Optional[EmbeddingStore]
) -> dict[str, IssueCluster]:
    """
    Cluster events into issue groups and mutate each event in-place with:
        event.issue_cluster_id
        event.related_event_ids
        event.issue_status

    Returns dict[cluster_id, IssueCluster].

    Events that don't link to anything form singleton clusters (each is its own issue).
    """
    if not events:
        return {}

    ids = [e.event_id for e in events]
    uf = _UnionFind(ids)

    event_map = {e.event_id: e for e in events}

    # Pairwise linking pass
    for i, ev_a in enumerate(events):
        for ev_b in events[i + 1:]:
            if _should_link(ev_a, ev_b, embedding_store):
                uf.union(ev_a.event_id, ev_b.event_id)

    # Build clusters from union-find groups
    groups = uf.groups()
    clusters: dict[str, IssueCluster] = {}

    for cluster_id, member_ids in groups.items():
        members = [event_map[eid] for eid in member_ids if eid in event_map]
        issue_status = _infer_issue_status(members)
        dominant_topic = _dominant_topic(members)

        # Representative = first event by start time (most likely the root cause)
        representative = min(members, key=lambda e: e.started_at)

        cluster = IssueCluster(
            cluster_id=cluster_id,
            event_ids=member_ids,
            dominant_topic=dominant_topic,
            representative_event_id=representative.event_id,
            issue_status=issue_status,
        )
        clusters[cluster_id] = cluster

    # Mutate events in-place
    for cluster_id, cluster in clusters.items():
        for eid in cluster.event_ids:
            ev = event_map.get(eid)
            if ev is None:
                continue
            ev.issue_cluster_id = cluster_id
            ev.related_event_ids = [
                x for x in cluster.event_ids if x != eid
            ]
            ev.issue_status = cluster.issue_status

    return clusters


def _should_link(ev_a, ev_b, embedding_store) -> bool:
    """Return True if two events should be grouped into the same issue cluster."""

    # 1. Entity overlap
    entities_a = _entity_set(ev_a)
    entities_b = _entity_set(ev_b)
    if len(entities_a & entities_b) >= ENTITY_OVERLAP_THRESHOLD:
        return True

    # 2. Semantic similarity via embedding store
    if embedding_store is not None:
        emb_a = embedding_store.get(ev_a.event_id)
        emb_b = embedding_store.get(ev_b.event_id)
        if emb_a is not None and emb_b is not None:
            import numpy as np
            dot = float(np.dot(emb_a, emb_b))
            sim = max(-1.0, min(1.0, dot))
            if sim >= SEMANTIC_SIM_THRESHOLD:
                return True

    # NOTE: Participant overlap is intentionally not used as a linking signal.
    # On small teams (≤10 users), nearly all thread pairs share 2+ participants,
    # causing transitive over-linking through union-find. Entity and semantic
    # signals are more discriminating and reliable.

    return False


def _entity_set(ev) -> set[str]:
    """
    Return high-value entity strings for an event.

    Only use specific entities (parts, revisions, builds, suppliers) for linking.
    Subsystems like "BMS" or "I2C" are excluded because they appear in many threads
    on small teams, causing over-linking via transitive closure in union-find.
    """
    if ev.signals is None or not ev.signals.extracted_entities:
        return set()
    # Only link on specific, discriminating entity types
    high_value_types = {"parts", "revisions", "builds", "suppliers"}
    result: set[str] = set()
    for etype, entities in ev.signals.extracted_entities.items():
        if etype in high_value_types:
            for e in entities:
                result.add(e.lower())
    return result


def _topic_set(ev) -> set[str]:
    if ev.signals is None:
        return set()
    return set(ev.signals.topic_labels)


def _infer_issue_status(members: list) -> str:
    """
    Infer whether an issue is new, ongoing, or resurfacing.

    New:         Single event, or all events within 6h of each other.
    Resurfacing: Events are separated by a quiet gap >12h followed by renewed activity.
    Ongoing:     Multiple events, spread over >6h, no quiet gap.
    """
    if len(members) <= 1:
        return "new"

    sorted_events = sorted(members, key=lambda e: e.started_at)
    first = sorted_events[0].started_at
    last = sorted_events[-1].last_activity_at

    total_span = (last - first).total_seconds() / 3600.0

    if total_span < 6.0:
        return "new"

    # Check for a resurfacing gap: consecutive events with >12h gap in between
    for i in range(1, len(sorted_events)):
        gap = (sorted_events[i].started_at - sorted_events[i - 1].last_activity_at)
        if gap > timedelta(hours=12):
            return "resurfacing"

    return "ongoing"


def _dominant_topic(members: list) -> str:
    """Most common topic label across cluster members."""
    counts: dict[str, int] = {}
    for ev in members:
        for label in _topic_set(ev):
            counts[label] = counts.get(label, 0) + 1
    if not counts:
        return ""
    return max(counts, key=counts.get)
