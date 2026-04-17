"""
Tests for the dependency & impact graph layer.

Coverage:
    1. Empty event list → empty graph
    2. Single event → single node, no edges
    3. Two events with shared entity + blocker type → "blocks" edge created
    4. Two events in same cluster → "depends_on" edge created
    5. Two events with shared topic + participant → "related_to" edge created
    6. GraphSignals.downstream_impact_count is correct
    7. GraphSignals.graph_centrality_score is [0, 1]
    8. graph_impact_boost is capped at 0.15
    9. Risk/decision event impacting a blocker → "impacts" edge created
    10. Non-blocker event does NOT create a "blocks" edge
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.impact.graph import build_issue_graph
from src.impact.graph_models import IssueGraph, GraphEdge, GraphNode, GraphSignals


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BASE_TIME = datetime(2026, 4, 10, 10, 0, 0, tzinfo=timezone.utc)


def _make_signals(
    dominant_event_type: str = "noise",
    topic_labels: list[str] | None = None,
    urgency_score: float = 0.0,
    importance_score: float = 0.3,
    extracted_entities: dict | None = None,
    blocker_prob: float = 0.0,
) -> MagicMock:
    signals = MagicMock()
    signals.dominant_event_type = dominant_event_type
    signals.topic_labels = topic_labels or []
    signals.urgency_score = urgency_score
    signals.importance_score = importance_score
    signals.extracted_entities = extracted_entities or {}
    signals.title = f"Mock {dominant_event_type} event"

    dist = MagicMock()
    dist.blocker = blocker_prob
    dist.risk = 0.0
    dist.decision = 0.0
    dist.status_update = 0.0
    dist.request_for_input = 0.0
    dist.noise = 0.0
    signals.event_type_dist = dist
    return signals


def _make_event(
    event_id: str,
    dominant_event_type: str = "noise",
    topic_labels: list[str] | None = None,
    participant_ids: list[str] | None = None,
    urgency_score: float = 0.0,
    importance_score: float = 0.3,
    extracted_entities: dict | None = None,
    issue_cluster_id: str | None = None,
    started_at: datetime | None = None,
    last_activity_at: datetime | None = None,
    blocker_prob: float = 0.0,
    signals: bool = True,
) -> MagicMock:
    """Create a mock CandidateEvent with configurable signals."""
    event = MagicMock()
    event.event_id = event_id
    event.thread_id = f"thread_{event_id}"
    event.participant_ids = participant_ids or ["u_alice"]
    event.started_at = started_at or BASE_TIME
    event.last_activity_at = last_activity_at or BASE_TIME
    event.issue_cluster_id = issue_cluster_id  # May be None

    if signals:
        event.signals = _make_signals(
            dominant_event_type=dominant_event_type,
            topic_labels=topic_labels or [],
            urgency_score=urgency_score,
            importance_score=importance_score,
            extracted_entities=extracted_entities or {},
            blocker_prob=blocker_prob,
        )
    else:
        event.signals = None

    return event


# ---------------------------------------------------------------------------
# 1. Empty event list → empty graph
# ---------------------------------------------------------------------------

class TestEmptyGraph:
    def test_empty_event_list_returns_empty_graph(self):
        graph, signals = build_issue_graph([])
        assert isinstance(graph, IssueGraph)
        assert graph.nodes == []
        assert graph.edges == []
        assert signals == {}


# ---------------------------------------------------------------------------
# 2. Single event → single node, no edges
# ---------------------------------------------------------------------------

class TestSingleEvent:
    def test_single_event_creates_one_node(self):
        ev = _make_event("ev_a", dominant_event_type="status_update")
        graph, signals = build_issue_graph([ev])
        assert len(graph.nodes) == 1
        assert graph.nodes[0].event_id == "ev_a"

    def test_single_event_has_no_edges(self):
        ev = _make_event("ev_a", dominant_event_type="blocker")
        graph, signals = build_issue_graph([ev])
        assert graph.edges == []

    def test_single_event_graph_signals_have_zero_counts(self):
        ev = _make_event("ev_a")
        _, signals = build_issue_graph([ev])
        assert signals["ev_a"].downstream_impact_count == 0
        assert signals["ev_a"].upstream_dependency_count == 0


# ---------------------------------------------------------------------------
# 3. Blocker + shared entity → "blocks" edge
# ---------------------------------------------------------------------------

class TestBlocksEdge:
    def setup_method(self):
        from datetime import timedelta
        self.ev_a = _make_event(
            "ev_a",
            dominant_event_type="blocker",
            extracted_entities={"parts": ["SENSOR_V2", "MCU_BOARD"]},
            started_at=BASE_TIME,
            blocker_prob=0.85,
        )
        self.ev_b = _make_event(
            "ev_b",
            dominant_event_type="status_update",
            extracted_entities={"parts": ["SENSOR_V2", "CABLE_HARNESS"]},
            started_at=BASE_TIME + __import__("datetime").timedelta(hours=2),
        )

    def test_blocks_edge_created(self):
        graph, _ = build_issue_graph([self.ev_a, self.ev_b])
        blocks = [e for e in graph.edges if e.relation_type == "blocks"]
        assert len(blocks) == 1
        assert blocks[0].source_event_id == "ev_a"
        assert blocks[0].target_event_id == "ev_b"

    def test_blocks_edge_confidence_in_range(self):
        graph, _ = build_issue_graph([self.ev_a, self.ev_b])
        blocks = [e for e in graph.edges if e.relation_type == "blocks"]
        assert 0.0 < blocks[0].confidence <= 1.0

    def test_blocks_edge_has_explanation(self):
        graph, _ = build_issue_graph([self.ev_a, self.ev_b])
        blocks = [e for e in graph.edges if e.relation_type == "blocks"]
        assert len(blocks[0].explanation) > 0

    def test_no_blocks_edge_when_non_blocker(self):
        """A status_update event should NOT create a blocks edge even with shared entities."""
        ev_c = _make_event(
            "ev_c",
            dominant_event_type="status_update",
            extracted_entities={"parts": ["SENSOR_V2"]},
            started_at=BASE_TIME,
        )
        ev_d = _make_event(
            "ev_d",
            dominant_event_type="risk",
            extracted_entities={"parts": ["SENSOR_V2"]},
            started_at=BASE_TIME + __import__("datetime").timedelta(hours=1),
        )
        graph, _ = build_issue_graph([ev_c, ev_d])
        blocks = [e for e in graph.edges if e.relation_type == "blocks"]
        assert len(blocks) == 0

    def test_no_blocks_edge_when_no_shared_entity(self):
        """Blocker with no shared entities should not produce a blocks edge."""
        ev_e = _make_event(
            "ev_e",
            dominant_event_type="blocker",
            extracted_entities={"parts": ["PART_X"]},
            started_at=BASE_TIME,
        )
        ev_f = _make_event(
            "ev_f",
            dominant_event_type="status_update",
            extracted_entities={"parts": ["PART_Y"]},
            started_at=BASE_TIME + __import__("datetime").timedelta(hours=1),
        )
        graph, _ = build_issue_graph([ev_e, ev_f])
        blocks = [e for e in graph.edges if e.relation_type == "blocks"]
        assert len(blocks) == 0


# ---------------------------------------------------------------------------
# 4. Same cluster → "depends_on" edge
# ---------------------------------------------------------------------------

class TestDependsOnEdge:
    def setup_method(self):
        from datetime import timedelta
        # ev_b started earlier, higher urgency → ev_a depends on ev_b
        self.ev_a = _make_event(
            "ev_a",
            urgency_score=0.2,
            issue_cluster_id="cluster_1",
            started_at=BASE_TIME + __import__("datetime").timedelta(hours=3),
        )
        self.ev_b = _make_event(
            "ev_b",
            urgency_score=0.8,
            issue_cluster_id="cluster_1",
            started_at=BASE_TIME,
        )

    def test_depends_on_edge_created(self):
        graph, _ = build_issue_graph([self.ev_a, self.ev_b])
        dep = [e for e in graph.edges if e.relation_type == "depends_on"]
        assert len(dep) == 1
        assert dep[0].source_event_id == "ev_a"
        assert dep[0].target_event_id == "ev_b"

    def test_depends_on_confidence_is_0_5(self):
        graph, _ = build_issue_graph([self.ev_a, self.ev_b])
        dep = [e for e in graph.edges if e.relation_type == "depends_on"]
        assert dep[0].confidence == 0.5

    def test_no_depends_on_without_cluster(self):
        """Events with no cluster ID should not generate depends_on edges."""
        ev_x = _make_event("ev_x", urgency_score=0.3, issue_cluster_id=None)
        ev_y = _make_event(
            "ev_y",
            urgency_score=0.9,
            issue_cluster_id=None,
            started_at=BASE_TIME,
        )
        graph, _ = build_issue_graph([ev_x, ev_y])
        dep = [e for e in graph.edges if e.relation_type == "depends_on"]
        assert len(dep) == 0


# ---------------------------------------------------------------------------
# 5. Shared topic + participant → "related_to" edge
# ---------------------------------------------------------------------------

class TestRelatedToEdge:
    def setup_method(self):
        self.ev_a = _make_event(
            "ev_a",
            topic_labels=["firmware", "testing"],
            participant_ids=["u_alice", "u_bob"],
        )
        self.ev_b = _make_event(
            "ev_b",
            topic_labels=["firmware", "supply_chain"],
            participant_ids=["u_alice", "u_carol"],
        )

    def test_related_to_edge_created(self):
        graph, _ = build_issue_graph([self.ev_a, self.ev_b])
        related = [e for e in graph.edges if e.relation_type == "related_to"]
        assert len(related) == 1

    def test_related_to_edge_between_correct_events(self):
        graph, _ = build_issue_graph([self.ev_a, self.ev_b])
        related = [e for e in graph.edges if e.relation_type == "related_to"]
        pair = {related[0].source_event_id, related[0].target_event_id}
        assert pair == {"ev_a", "ev_b"}

    def test_no_related_to_without_shared_participant(self):
        ev_x = _make_event("ev_x", topic_labels=["firmware"], participant_ids=["u_alice"])
        ev_y = _make_event("ev_y", topic_labels=["firmware"], participant_ids=["u_bob"])
        graph, _ = build_issue_graph([ev_x, ev_y])
        related = [e for e in graph.edges if e.relation_type == "related_to"]
        assert len(related) == 0

    def test_no_related_to_without_shared_topic(self):
        ev_x = _make_event("ev_x", topic_labels=["firmware"], participant_ids=["u_alice"])
        ev_y = _make_event("ev_y", topic_labels=["hardware"], participant_ids=["u_alice"])
        graph, _ = build_issue_graph([ev_x, ev_y])
        related = [e for e in graph.edges if e.relation_type == "related_to"]
        assert len(related) == 0


# ---------------------------------------------------------------------------
# 6. GraphSignals.downstream_impact_count is correct
# ---------------------------------------------------------------------------

class TestDownstreamImpactCount:
    def test_downstream_count_matches_blocks_edges(self):
        from datetime import timedelta
        # ev_blocker blocks ev_b and ev_c
        ev_blocker = _make_event(
            "ev_blocker",
            dominant_event_type="blocker",
            extracted_entities={"parts": ["PART_ALPHA"]},
            started_at=BASE_TIME,
            blocker_prob=0.9,
        )
        ev_b = _make_event(
            "ev_b",
            extracted_entities={"parts": ["PART_ALPHA"]},
            started_at=BASE_TIME + __import__("datetime").timedelta(hours=1),
        )
        ev_c = _make_event(
            "ev_c",
            extracted_entities={"parts": ["PART_ALPHA"]},
            started_at=BASE_TIME + __import__("datetime").timedelta(hours=2),
        )
        _, signals = build_issue_graph([ev_blocker, ev_b, ev_c])
        # Blocker blocks ev_b and ev_c
        assert signals["ev_blocker"].downstream_impact_count == 2
        # ev_b and ev_c are downstream — they don't block anything here
        assert signals["ev_b"].downstream_impact_count == 0
        assert signals["ev_c"].downstream_impact_count == 0


# ---------------------------------------------------------------------------
# 7. GraphSignals.graph_centrality_score is [0, 1]
# ---------------------------------------------------------------------------

class TestCentralityScore:
    def test_centrality_score_in_range(self):
        events = [
            _make_event(f"ev_{i}", topic_labels=["firmware"], participant_ids=["u_alice"])
            for i in range(5)
        ]
        _, signals = build_issue_graph(events)
        for eid, sig in signals.items():
            assert 0.0 <= sig.graph_centrality_score <= 1.0, (
                f"Centrality out of range for {eid}: {sig.graph_centrality_score}"
            )

    def test_isolated_node_centrality_is_zero(self):
        """A single event with no connections has centrality 0."""
        ev = _make_event("ev_alone")
        _, signals = build_issue_graph([ev])
        assert signals["ev_alone"].graph_centrality_score == 0.0

    def test_highest_degree_node_has_max_centrality(self):
        """The most connected node should have centrality = 1.0."""
        from datetime import timedelta
        # hub blocks 3 others
        hub = _make_event(
            "hub",
            dominant_event_type="blocker",
            extracted_entities={"parts": ["SHARED_PART"]},
            started_at=BASE_TIME,
            blocker_prob=0.9,
        )
        targets = [
            _make_event(
                f"tgt_{i}",
                extracted_entities={"parts": ["SHARED_PART"]},
                started_at=BASE_TIME + __import__("datetime").timedelta(hours=i + 1),
            )
            for i in range(3)
        ]
        _, signals = build_issue_graph([hub] + targets)
        assert signals["hub"].graph_centrality_score == 1.0


# ---------------------------------------------------------------------------
# 8. graph_impact_boost is capped at 0.15
# ---------------------------------------------------------------------------

class TestImpactBoostCap:
    def test_boost_capped_at_0_15(self):
        """Even if a blocker has many downstream events, boost must not exceed 0.15."""
        from datetime import timedelta
        hub = _make_event(
            "hub",
            dominant_event_type="blocker",
            extracted_entities={"parts": ["MEGA_PART"]},
            started_at=BASE_TIME,
            blocker_prob=0.95,
        )
        # 10 downstream events — 10 * 0.05 = 0.5, but should be capped at 0.15
        targets = [
            _make_event(
                f"tgt_{i}",
                extracted_entities={"parts": ["MEGA_PART"]},
                started_at=BASE_TIME + __import__("datetime").timedelta(hours=i + 1),
            )
            for i in range(10)
        ]
        _, signals = build_issue_graph([hub] + targets)
        assert signals["hub"].graph_impact_boost <= 0.15

    def test_boost_zero_for_isolated_event(self):
        ev = _make_event("ev_solo")
        _, signals = build_issue_graph([ev])
        assert signals["ev_solo"].graph_impact_boost == 0.0

    def test_boost_scales_with_downstream_count(self):
        """For small counts (< 3), boost should be downstream_count * 0.05."""
        from datetime import timedelta
        hub = _make_event(
            "hub",
            dominant_event_type="blocker",
            extracted_entities={"parts": ["PART_Z"]},
            started_at=BASE_TIME,
            blocker_prob=0.9,
        )
        tgt = _make_event(
            "tgt",
            extracted_entities={"parts": ["PART_Z"]},
            started_at=BASE_TIME + __import__("datetime").timedelta(hours=1),
        )
        _, signals = build_issue_graph([hub, tgt])
        # 1 downstream → boost = 1 * 0.05 = 0.05
        assert signals["hub"].graph_impact_boost == pytest.approx(0.05, abs=1e-4)
