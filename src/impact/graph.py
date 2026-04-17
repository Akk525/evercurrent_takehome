"""
Dependency & impact graph construction over enriched CandidateEvents.

Infers directed edges between events based on signals already present after
enrichment (dominant event type, topic labels, entities, cluster membership,
urgency scores). No re-enrichment happens here.

Edge types:
    blocks     — A appears to block B (A is a blocker, shares entities with B,
                 and started before B)
    depends_on — A depends on B (same cluster, B is more urgent and started
                 before A)
    related_to — A and B share topic labels AND at least one participant, but
                 are not already connected by a directional edge
    impacts    — A (risk/decision) may affect B (blocker) in the same topic cluster
                 (lower confidence, no shared entity required)

All inferences are probabilistic — confidence scores reflect uncertainty.
This module never modifies existing CandidateEvent objects.
"""

from __future__ import annotations

from typing import Optional

from .graph_models import GraphEdge, GraphNode, GraphSignals, IssueGraph


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_issue_graph(
    events: list,          # list[CandidateEvent] — typed loosely to avoid circular import
    embedding_store=None,  # Optional[EmbeddingStore] — reserved for future use
) -> tuple[IssueGraph, dict[str, GraphSignals]]:
    """
    Build a dependency & impact graph over a list of enriched CandidateEvents.

    Returns:
        (IssueGraph, dict[event_id, GraphSignals])

    The IssueGraph contains GraphNode and GraphEdge objects.
    The dict maps each event_id to its graph-derived signals (boost, centrality, etc.).

    Defensive: handles None signals, None issue_cluster_id, missing attributes.
    """
    if not events:
        return IssueGraph(nodes=[], edges=[]), {}

    event_map = {e.event_id: e for e in events}

    # Build edges in order of specificity / confidence
    edges: list[GraphEdge] = []
    connected_pairs: set[tuple[str, str]] = set()  # Track directed pairs already covered

    # Pass 1: "blocks" edges
    blocks_edges = _infer_blocks_edges(events)
    for edge in blocks_edges:
        edges.append(edge)
        connected_pairs.add((edge.source_event_id, edge.target_event_id))

    # Pass 2: "depends_on" edges
    depends_edges = _infer_depends_on_edges(events)
    for edge in depends_edges:
        pair = (edge.source_event_id, edge.target_event_id)
        if pair not in connected_pairs:
            edges.append(edge)
            connected_pairs.add(pair)

    # Pass 3: "related_to" edges (undirected — add both directions to connected_pairs)
    related_edges = _infer_related_to_edges(events, connected_pairs)
    for edge in related_edges:
        edges.append(edge)
        connected_pairs.add((edge.source_event_id, edge.target_event_id))
        connected_pairs.add((edge.target_event_id, edge.source_event_id))

    # Pass 4: "impacts" edges (lowest confidence, only where no edge yet)
    impacts_edges = _infer_impacts_edges(events, connected_pairs)
    for edge in impacts_edges:
        pair = (edge.source_event_id, edge.target_event_id)
        if pair not in connected_pairs:
            edges.append(edge)
            connected_pairs.add(pair)

    # Compute per-event graph signals
    graph_signals = _compute_graph_signals(events, edges)

    # Build nodes
    nodes = _build_nodes(events, edges, graph_signals)

    return IssueGraph(nodes=nodes, edges=edges), graph_signals


# ---------------------------------------------------------------------------
# Edge inference helpers
# ---------------------------------------------------------------------------

def _infer_blocks_edges(events: list) -> list[GraphEdge]:
    """
    Edge A → B (blocks) when:
        - A has dominant_event_type == "blocker"
        - A and B share at least one high-value entity
        - A started before B temporally

    confidence = 0.4 + 0.3 * entity_overlap_ratio + 0.3 * type_confidence
    """
    edges = []
    for i, ev_a in enumerate(events):
        if _dominant_type(ev_a) != "blocker":
            continue
        entities_a = _entity_set(ev_a)
        if not entities_a:
            continue
        type_conf_a = _type_confidence(ev_a, "blocker")

        for ev_b in events:
            if ev_b.event_id == ev_a.event_id:
                continue
            if ev_a.started_at >= ev_b.started_at:
                continue  # A must precede B

            entities_b = _entity_set(ev_b)
            if not entities_b:
                continue

            shared = entities_a & entities_b
            if not shared:
                continue

            overlap_ratio = len(shared) / max(len(entities_a), len(entities_b))
            confidence = round(
                min(1.0, 0.4 + 0.3 * overlap_ratio + 0.3 * type_conf_a), 3
            )

            entity_str = ", ".join(sorted(shared)[:3])
            explanation = (
                f"Event '{ev_a.event_id}' appears to block '{ev_b.event_id}': "
                f"both reference {entity_str} and the potential blocker started earlier. "
                f"Entity overlap: {len(shared)} shared item(s)."
            )
            edges.append(GraphEdge(
                source_event_id=ev_a.event_id,
                target_event_id=ev_b.event_id,
                relation_type="blocks",
                confidence=confidence,
                explanation=explanation,
            ))
    return edges


def _infer_depends_on_edges(events: list) -> list[GraphEdge]:
    """
    Edge A → B (depends_on) when:
        - A and B share the same issue_cluster_id (non-None)
        - B's urgency_score > A's urgency_score
        - B started before A

    confidence = 0.5 * cluster_overlap (fixed at 0.5 since cluster is binary)
    """
    edges = []
    for i, ev_a in enumerate(events):
        cluster_a = _cluster_id(ev_a)
        if cluster_a is None:
            continue
        urgency_a = _urgency(ev_a)

        for ev_b in events:
            if ev_b.event_id == ev_a.event_id:
                continue
            cluster_b = _cluster_id(ev_b)
            if cluster_b != cluster_a:
                continue

            urgency_b = _urgency(ev_b)
            if urgency_b <= urgency_a:
                continue  # B must be more urgent
            if ev_b.started_at >= ev_a.started_at:
                continue  # B must precede A

            confidence = 0.5  # Binary cluster membership

            explanation = (
                f"Event '{ev_a.event_id}' likely depends on '{ev_b.event_id}': "
                f"both belong to issue cluster '{cluster_a}', and the dependency "
                f"appears more urgent (urgency {urgency_b:.2f} vs {urgency_a:.2f})."
            )
            edges.append(GraphEdge(
                source_event_id=ev_a.event_id,
                target_event_id=ev_b.event_id,
                relation_type="depends_on",
                confidence=confidence,
                explanation=explanation,
            ))
    return edges


def _infer_related_to_edges(
    events: list, existing_pairs: set[tuple[str, str]]
) -> list[GraphEdge]:
    """
    Undirected edge A — B (related_to) when:
        - A and B share ≥1 topic_label
        - A and B share ≥1 participant
        - Not already connected by blocks/depends_on

    confidence = 0.3 + 0.1 * min(shared_topics, 3) + 0.1 * min(shared_participants, 3)
    """
    edges = []
    seen: set[frozenset] = set()  # Avoid duplicate undirected pairs

    for i, ev_a in enumerate(events):
        topics_a = _topic_set(ev_a)
        participants_a = set(ev_a.participant_ids)

        for ev_b in events[i + 1:]:
            pair_key = frozenset({ev_a.event_id, ev_b.event_id})
            if pair_key in seen:
                continue

            # Skip if already connected directionally
            if (ev_a.event_id, ev_b.event_id) in existing_pairs:
                continue
            if (ev_b.event_id, ev_a.event_id) in existing_pairs:
                continue

            topics_b = _topic_set(ev_b)
            participants_b = set(ev_b.participant_ids)

            shared_topics = topics_a & topics_b
            shared_participants = participants_a & participants_b

            if not shared_topics or not shared_participants:
                continue

            confidence = round(
                min(1.0, 0.3 + 0.1 * min(len(shared_topics), 3)
                    + 0.1 * min(len(shared_participants), 3)), 3
            )

            topic_str = ", ".join(sorted(shared_topics)[:3])
            explanation = (
                f"Events '{ev_a.event_id}' and '{ev_b.event_id}' appear related: "
                f"they share topic(s) [{topic_str}] and {len(shared_participants)} "
                f"common participant(s)."
            )
            edges.append(GraphEdge(
                source_event_id=ev_a.event_id,
                target_event_id=ev_b.event_id,
                relation_type="related_to",
                confidence=confidence,
                explanation=explanation,
            ))
            seen.add(pair_key)
    return edges


def _infer_impacts_edges(
    events: list, existing_pairs: set[tuple[str, str]]
) -> list[GraphEdge]:
    """
    Edge A → B (impacts) when:
        - A is a "risk" or "decision" event
        - B is a "blocker" event
        - A and B share ≥1 topic_label (same topic cluster)
        - Not already connected

    confidence = 0.25 (low — no entity/cluster overlap required)
    """
    edges = []
    for ev_a in events:
        if _dominant_type(ev_a) not in ("risk", "decision"):
            continue
        topics_a = _topic_set(ev_a)
        if not topics_a:
            continue

        for ev_b in events:
            if ev_b.event_id == ev_a.event_id:
                continue
            if _dominant_type(ev_b) != "blocker":
                continue

            pair = (ev_a.event_id, ev_b.event_id)
            if pair in existing_pairs:
                continue

            topics_b = _topic_set(ev_b)
            shared = topics_a & topics_b
            if not shared:
                continue

            topic_str = ", ".join(sorted(shared)[:2])
            explanation = (
                f"Event '{ev_a.event_id}' (type: {_dominant_type(ev_a)}) may affect "
                f"'{ev_b.event_id}' (blocker) — both involve topic(s) [{topic_str}]. "
                f"Low-confidence inference: no entity overlap required."
            )
            edges.append(GraphEdge(
                source_event_id=ev_a.event_id,
                target_event_id=ev_b.event_id,
                relation_type="impacts",
                confidence=0.25,
                explanation=explanation,
            ))
    return edges


# ---------------------------------------------------------------------------
# Signal computation
# ---------------------------------------------------------------------------

def _compute_graph_signals(
    events: list, edges: list[GraphEdge]
) -> dict[str, GraphSignals]:
    """
    Derive per-event GraphSignals from the constructed edge set.

    - downstream_impact_count: edges where event is source of "blocks" or "impacts"
    - upstream_dependency_count: edges where event is TARGET of "blocks"
    - graph_centrality_score: normalized degree centrality in [0, 1]
    - graph_impact_boost: min(0.15, downstream_impact_count * 0.05)
    """
    event_ids = [e.event_id for e in events]
    n = len(event_ids)

    # Accumulate adjacency info
    downstream_blocks: dict[str, list[str]] = {eid: [] for eid in event_ids}
    upstream_blocked_by: dict[str, list[str]] = {eid: [] for eid in event_ids}
    degree: dict[str, int] = {eid: 0 for eid in event_ids}

    for edge in edges:
        src, tgt = edge.source_event_id, edge.target_event_id

        # Degree centrality: count each endpoint
        if src in degree:
            degree[src] += 1
        if tgt in degree:
            degree[tgt] += 1

        if edge.relation_type in ("blocks", "impacts"):
            if src in downstream_blocks:
                downstream_blocks[src].append(tgt)
        if edge.relation_type == "blocks":
            if tgt in upstream_blocked_by:
                upstream_blocked_by[tgt].append(src)

    max_degree = max(degree.values()) if degree else 0

    signals: dict[str, GraphSignals] = {}
    for eid in event_ids:
        d_count = len(downstream_blocks[eid])
        u_count = len(upstream_blocked_by[eid])
        raw_degree = degree[eid]
        centrality = raw_degree / max_degree if max_degree > 0 else 0.0
        boost = min(0.15, d_count * 0.05)

        signals[eid] = GraphSignals(
            downstream_impact_count=d_count,
            upstream_dependency_count=u_count,
            blocks_event_ids=list(downstream_blocks[eid]),
            depends_on_event_ids=list(upstream_blocked_by[eid]),
            graph_centrality_score=round(centrality, 4),
            graph_impact_boost=round(boost, 4),
        )

    return signals


def _build_nodes(
    events: list,
    edges: list[GraphEdge],
    graph_signals: dict[str, GraphSignals],
) -> list[GraphNode]:
    """Build GraphNode objects from events and computed signals."""
    nodes = []
    for ev in events:
        sig = graph_signals.get(ev.event_id, GraphSignals())
        title = _title(ev)
        event_type = _dominant_type(ev)
        importance = _importance(ev)

        if importance >= 0.7:
            signal_level = "high"
        elif importance >= 0.4:
            signal_level = "medium"
        else:
            signal_level = "low"

        nodes.append(GraphNode(
            event_id=ev.event_id,
            title=title,
            signal_level=signal_level,
            dominant_event_type=event_type,
            issue_cluster_id=_cluster_id(ev),
            upstream_count=sig.upstream_dependency_count,
            downstream_count=sig.downstream_impact_count,
            centrality_score=sig.graph_centrality_score,
        ))
    return nodes


# ---------------------------------------------------------------------------
# Attribute accessors (defensive — handle None signals gracefully)
# ---------------------------------------------------------------------------

def _dominant_type(ev) -> str:
    if ev.signals is None:
        return "noise"
    return getattr(ev.signals, "dominant_event_type", "noise")


def _entity_set(ev) -> set[str]:
    """Return high-value entities from extracted_entities if available."""
    if ev.signals is None:
        return set()
    entities = getattr(ev.signals, "extracted_entities", None) or {}
    high_value_types = {"parts", "revisions", "builds", "suppliers"}
    result: set[str] = set()
    for etype, items in entities.items():
        if etype in high_value_types:
            for item in (items or []):
                result.add(str(item).lower())
    return result


def _topic_set(ev) -> set[str]:
    if ev.signals is None:
        return set()
    return set(getattr(ev.signals, "topic_labels", []) or [])


def _urgency(ev) -> float:
    if ev.signals is None:
        return 0.0
    return getattr(ev.signals, "urgency_score", 0.0)


def _importance(ev) -> float:
    if ev.signals is None:
        return 0.0
    return getattr(ev.signals, "importance_score", 0.0)


def _cluster_id(ev) -> Optional[str]:
    return getattr(ev, "issue_cluster_id", None)


def _title(ev) -> str:
    if ev.signals is None:
        return ev.event_id
    return getattr(ev.signals, "title", ev.event_id)


def _type_confidence(ev, event_type: str) -> float:
    """Return the probability score for a specific event type from event_type_dist."""
    if ev.signals is None:
        return 0.0
    dist = getattr(ev.signals, "event_type_dist", None)
    if dist is None:
        return 0.0
    return getattr(dist, event_type, 0.0)
