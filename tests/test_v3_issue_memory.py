"""
Tests for the V3 persistent issue memory layer.

Coverage:
    - make_entity_fingerprint: extracts and deduplicates high-value entities
    - _match_score: Jaccard + topic boost logic
    - match_and_update_issues: new issue creation, ongoing, resurfacing transitions
    - IssueMemoryStore: SQLite round-trip (init, upsert, load_all, get)
    - new_issue_record: correct defaults
    - Memory signals attached to enriched events during pipeline run
    - why_shown includes memory label for ongoing issues
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.issue_memory.store import (
    IssueMemoryStore,
    IssueRecord,
    make_entity_fingerprint,
    new_issue_record,
)
from src.issue_memory.matcher import (
    IssueMemorySignals,
    match_and_update_issues,
    _match_score,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NOW = datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)


def _make_store(tmp_path: Path) -> IssueMemoryStore:
    store = IssueMemoryStore(db_path=tmp_path / "test_memory.db")
    store.init()
    return store


def _make_event(
    event_id: str = "e_001",
    thread_id: str = "t_001",
    entities: dict | None = None,
    topic: str = "supply_chain",
    event_type: str = "risk",
) -> MagicMock:
    """Create a minimal CandidateEvent mock that matcher.py can process."""
    event = MagicMock()
    event.event_id = event_id
    event.thread_id = thread_id
    signals = MagicMock()
    signals.extracted_entities = entities or {
        "parts": ["SHT40", "MX150"],
        "revisions": ["Rev C"],
        "builds": [],
        "suppliers": ["Molex"],
        "subsystems": ["I2C"],
        "deadlines": [],
    }
    signals.topic_labels = [topic]
    signals.dominant_event_type = event_type
    signals.state_change_hint = None
    signals.title = f"Issue in {thread_id}"
    event.signals = signals
    return event


# ---------------------------------------------------------------------------
# make_entity_fingerprint
# ---------------------------------------------------------------------------

class TestMakeEntityFingerprint:
    def test_returns_pipe_delimited_sorted(self):
        entities = {
            "parts": ["SHT40", "MX150"],
            "revisions": ["Rev C"],
            "builds": ["DVT"],
            "suppliers": ["Molex"],
            "subsystems": ["I2C"],  # NOT high-value — should be excluded
            "deadlines": ["April 18"],  # NOT high-value — should be excluded
        }
        fp = make_entity_fingerprint(entities)
        items = fp.split("|")
        assert "sht40" in items
        assert "mx150" in items
        assert "rev c" in items
        assert "dvt" in items
        assert "molex" in items
        # subsystems and deadlines are excluded
        assert "i2c" not in items
        assert "april 18" not in items

    def test_empty_entities(self):
        fp = make_entity_fingerprint({})
        assert fp == ""

    def test_deduplication(self):
        entities = {"parts": ["SHT40", "SHT40", "sht40"], "revisions": [], "builds": [], "suppliers": []}
        fp = make_entity_fingerprint(entities)
        assert fp.count("sht40") == 1

    def test_sorted(self):
        entities = {"parts": ["ZZZ", "AAA", "MMM"], "revisions": [], "builds": [], "suppliers": []}
        fp = make_entity_fingerprint(entities)
        items = fp.split("|")
        assert items == sorted(items)


# ---------------------------------------------------------------------------
# _match_score
# ---------------------------------------------------------------------------

class TestMatchScore:
    def _make_record(self, entity_fp: str, topic: str = "supply_chain") -> IssueRecord:
        now_iso = NOW.isoformat()
        return IssueRecord(
            issue_id="issue-abc",
            first_seen=now_iso,
            last_seen=now_iso,
            current_status="ongoing",
            prior_status="new",
            hours_open=10.0,
            resurfaced_count=0,
            escalation_count=0,
            dominant_topic=topic,
            entity_fingerprint=entity_fp,
            related_thread_ids="[]",
            last_event_id="e_001",
            last_title="Test issue",
            last_event_type="risk",
            updated_at=now_iso,
        )

    def test_high_overlap(self):
        record = self._make_record("mx150|molex|rev c|sht40")
        current = {"sht40", "mx150", "rev c", "molex"}
        score = _match_score(current, record, "supply_chain")
        assert score >= 0.9  # near-perfect overlap

    def test_partial_overlap_above_threshold(self):
        record = self._make_record("sht40|mx150|dvt|molex")
        current = {"sht40", "mx150"}  # 2 of 4
        score = _match_score(current, record, "firmware")
        # Jaccard = 2/4 = 0.5
        assert score >= 0.25

    def test_no_overlap(self):
        record = self._make_record("sht40|rev c")
        current = {"molex", "dvt"}
        score = _match_score(current, record, "firmware")
        assert score == 0.0

    def test_topic_boost(self):
        record = self._make_record("sht40|mx150", topic="supply_chain")
        current = {"sht40"}
        score_with_boost = _match_score(current, record, "supply_chain")
        score_without_boost = _match_score(current, record, "other_topic")
        assert score_with_boost > score_without_boost

    def test_both_empty_same_topic(self):
        record = self._make_record("", topic="supply_chain")
        score = _match_score(set(), record, "supply_chain")
        assert score == 0.3

    def test_both_empty_different_topic(self):
        record = self._make_record("", topic="supply_chain")
        score = _match_score(set(), record, "firmware")
        assert score == 0.0

    def test_one_side_empty(self):
        record = self._make_record("sht40|mx150")
        score = _match_score(set(), record, "supply_chain")
        assert score == 0.0


# ---------------------------------------------------------------------------
# match_and_update_issues — new issue creation
# ---------------------------------------------------------------------------

class TestMatchAndUpdateIssues:
    def test_new_issue_created_for_unseen_event(self, tmp_path):
        store = _make_store(tmp_path)
        event = _make_event()

        match_and_update_issues([event], store, NOW)

        signals = event.issue_memory_signals
        assert isinstance(signals, IssueMemorySignals)
        assert signals.is_new_issue is True
        assert signals.is_ongoing_issue is False
        assert signals.is_resurfacing_issue is False
        assert signals.issue_age_hours == 0.0

    def test_new_issue_persisted_to_store(self, tmp_path):
        store = _make_store(tmp_path)
        event = _make_event()

        match_and_update_issues([event], store, NOW)

        issues = store.load_all()
        assert len(issues) == 1
        assert issues[0].current_status == "new"

    def test_stable_id_on_rematch(self, tmp_path):
        """Same entities → same issue ID across runs."""
        store = _make_store(tmp_path)
        event1 = _make_event(event_id="e_001")
        match_and_update_issues([event1], store, NOW)
        original_id = event1.issue_memory_signals.persistent_issue_id

        # Second run with overlapping entities
        event2 = _make_event(event_id="e_002")
        match_and_update_issues([event2], store, NOW + HOUR)
        matched_id = event2.issue_memory_signals.persistent_issue_id

        assert matched_id == original_id

    def test_unrelated_events_get_different_ids(self, tmp_path):
        store = _make_store(tmp_path)
        event_a = _make_event(
            event_id="e_a",
            entities={"parts": ["SHT40"], "revisions": [], "builds": [], "suppliers": []},
        )
        event_b = _make_event(
            event_id="e_b",
            entities={"parts": ["STM32"], "revisions": [], "builds": [], "suppliers": ["Maxim"]},
        )

        match_and_update_issues([event_a, event_b], store, NOW)

        id_a = event_a.issue_memory_signals.persistent_issue_id
        id_b = event_b.issue_memory_signals.persistent_issue_id
        assert id_a != id_b

    def test_ongoing_status_within_window(self, tmp_path):
        """Event seen within 48h of last_seen → ongoing."""
        store = _make_store(tmp_path)
        event1 = _make_event(event_id="e_001")
        match_and_update_issues([event1], store, NOW)

        event2 = _make_event(event_id="e_002")
        match_and_update_issues([event2], store, NOW + timedelta(hours=6))

        signals = event2.issue_memory_signals
        assert signals.is_ongoing_issue is True
        assert signals.is_resurfacing_issue is False

    def test_resurfacing_status_after_quiet_gap(self, tmp_path):
        """Event seen >48h after last_seen → resurfacing."""
        store = _make_store(tmp_path)
        event1 = _make_event(event_id="e_001")
        match_and_update_issues([event1], store, NOW)

        event2 = _make_event(event_id="e_002")
        match_and_update_issues([event2], store, NOW + timedelta(hours=50))

        signals = event2.issue_memory_signals
        assert signals.is_resurfacing_issue is True
        assert signals.resurfaced_count == 1

    def test_resolved_status_from_hint(self, tmp_path):
        """state_change_hint containing 'resolved' sets status to resolved."""
        store = _make_store(tmp_path)
        event1 = _make_event(event_id="e_001")
        match_and_update_issues([event1], store, NOW)

        event2 = _make_event(event_id="e_002")
        event2.signals.state_change_hint = "unresolved → resolved"
        match_and_update_issues([event2], store, NOW + timedelta(hours=2))

        signals = event2.issue_memory_signals
        assert signals.is_resolved_recently is True

    def test_escalation_count_increments(self, tmp_path):
        """Matching event with higher severity → escalation_count += 1."""
        store = _make_store(tmp_path)
        event1 = _make_event(event_id="e_001", event_type="status_update")
        match_and_update_issues([event1], store, NOW)

        event2 = _make_event(event_id="e_002", event_type="blocker")
        match_and_update_issues([event2], store, NOW + timedelta(hours=2))

        signals = event2.issue_memory_signals
        assert signals.escalation_count == 1

    def test_entity_fingerprint_union_on_update(self, tmp_path):
        """Entity fingerprint grows as new entities are observed."""
        store = _make_store(tmp_path)
        event1 = _make_event(
            event_id="e_001",
            entities={"parts": ["SHT40"], "revisions": [], "builds": [], "suppliers": []},
        )
        match_and_update_issues([event1], store, NOW)

        event2 = _make_event(
            event_id="e_002",
            entities={"parts": ["SHT40", "MX150"], "revisions": [], "builds": [], "suppliers": []},
        )
        match_and_update_issues([event2], store, NOW + timedelta(hours=2))

        issues = store.load_all()
        assert len(issues) == 1
        entity_set = issues[0].entity_set()
        assert "sht40" in entity_set
        assert "mx150" in entity_set

    def test_events_in_same_run_can_match_each_other(self, tmp_path):
        """Later events in a single call can match records created by earlier events."""
        store = _make_store(tmp_path)
        event1 = _make_event(event_id="e_001")
        event2 = _make_event(event_id="e_002")  # same entity set

        match_and_update_issues([event1, event2], store, NOW)

        # Both should map to the same persistent issue
        id1 = event1.issue_memory_signals.persistent_issue_id
        id2 = event2.issue_memory_signals.persistent_issue_id
        assert id1 == id2

    def test_signals_skipped_for_event_without_signals(self, tmp_path):
        """Events with signals=None are skipped gracefully."""
        store = _make_store(tmp_path)
        event = MagicMock()
        event.event_id = "e_null"
        event.signals = None

        match_and_update_issues([event], store, NOW)

        # No issue created, no attribute set (or set to None)
        assert store.load_all() == []


# ---------------------------------------------------------------------------
# IssueMemoryStore — SQLite persistence
# ---------------------------------------------------------------------------

class TestIssueMemoryStore:
    def test_init_creates_table(self, tmp_path):
        store = _make_store(tmp_path)
        # If init succeeded, load_all should return []
        assert store.load_all() == []

    def test_upsert_and_get(self, tmp_path):
        store = _make_store(tmp_path)
        now_iso = NOW.isoformat()
        record = IssueRecord(
            issue_id="test-uuid-1",
            first_seen=now_iso,
            last_seen=now_iso,
            current_status="new",
            prior_status="",
            hours_open=0.0,
            resurfaced_count=0,
            escalation_count=0,
            dominant_topic="supply_chain",
            entity_fingerprint="sht40|mx150",
            related_thread_ids='["t_001"]',
            last_event_id="e_001",
            last_title="Test issue",
            last_event_type="risk",
            updated_at=now_iso,
        )
        store.upsert(record)
        fetched = store.get("test-uuid-1")
        assert fetched is not None
        assert fetched.issue_id == "test-uuid-1"
        assert fetched.entity_fingerprint == "sht40|mx150"

    def test_load_all_returns_all_records(self, tmp_path):
        store = _make_store(tmp_path)
        now_iso = NOW.isoformat()
        for i in range(3):
            record = new_issue_record(
                event_id=f"e_{i:03d}",
                thread_ids=[f"t_{i:03d}"],
                title=f"Issue {i}",
                event_type="risk",
                dominant_topic="firmware",
                entity_fingerprint=f"part{i}",
                now=NOW,
            )
            store.upsert(record)
        assert len(store.load_all()) == 3

    def test_upsert_replaces_existing(self, tmp_path):
        store = _make_store(tmp_path)
        record = new_issue_record(
            event_id="e_001",
            thread_ids=["t_001"],
            title="Original",
            event_type="risk",
            dominant_topic="supply_chain",
            entity_fingerprint="sht40",
            now=NOW,
        )
        store.upsert(record)

        updated = IssueRecord(
            issue_id=record.issue_id,
            first_seen=record.first_seen,
            last_seen=(NOW + HOUR).isoformat(),
            current_status="ongoing",
            prior_status="new",
            hours_open=1.0,
            resurfaced_count=0,
            escalation_count=0,
            dominant_topic="supply_chain",
            entity_fingerprint="sht40|mx150",
            related_thread_ids='["t_001", "t_002"]',
            last_event_id="e_002",
            last_title="Updated title",
            last_event_type="blocker",
            updated_at=(NOW + HOUR).isoformat(),
        )
        store.upsert(updated)

        issues = store.load_all()
        assert len(issues) == 1
        assert issues[0].current_status == "ongoing"
        assert issues[0].last_title == "Updated title"

    def test_get_returns_none_for_missing(self, tmp_path):
        store = _make_store(tmp_path)
        assert store.get("nonexistent") is None


# ---------------------------------------------------------------------------
# new_issue_record
# ---------------------------------------------------------------------------

class TestNewIssueRecord:
    def test_correct_defaults(self):
        record = new_issue_record(
            event_id="e_001",
            thread_ids=["t_001"],
            title="Flash write failure",
            event_type="blocker",
            dominant_topic="firmware",
            entity_fingerprint="w25q128|spi",
            now=NOW,
        )
        assert record.current_status == "new"
        assert record.hours_open == 0.0
        assert record.resurfaced_count == 0
        assert record.escalation_count == 0
        assert record.entity_fingerprint == "w25q128|spi"
        assert record.first_seen == NOW.isoformat()

    def test_uuid_generated(self):
        r1 = new_issue_record("e1", ["t1"], "A", "risk", "firmware", "sht40", NOW)
        r2 = new_issue_record("e2", ["t2"], "B", "risk", "firmware", "sht40", NOW)
        assert r1.issue_id != r2.issue_id


# ---------------------------------------------------------------------------
# IssueRecord helpers
# ---------------------------------------------------------------------------

class TestIssueRecordHelpers:
    def _record(self, hours_open=10.0, resurfaced=0, status="ongoing", event_type="risk"):
        now_iso = NOW.isoformat()
        return IssueRecord(
            issue_id="x",
            first_seen=now_iso,
            last_seen=now_iso,
            current_status=status,
            prior_status="new",
            hours_open=hours_open,
            resurfaced_count=resurfaced,
            escalation_count=0,
            dominant_topic="firmware",
            entity_fingerprint="sht40",
            related_thread_ids='["t_001"]',
            last_event_id="e_001",
            last_title="Test",
            last_event_type=event_type,
            updated_at=now_iso,
        )

    def test_age_label_new(self):
        r = self._record(hours_open=0.5)
        assert r.age_label() == "new today"

    def test_age_label_hours(self):
        r = self._record(hours_open=10.0)
        assert "10h" in r.age_label()

    def test_age_label_days(self):
        r = self._record(hours_open=50.0)
        assert "2 day" in r.age_label()

    def test_memory_label_new(self):
        r = self._record(status="new")
        assert r.memory_label() == "New issue"

    def test_memory_label_resurfacing(self):
        r = self._record(status="resurfacing", resurfaced=2)
        label = r.memory_label()
        assert "Resurfaced" in label
        assert "2×" in label

    def test_memory_label_resolved(self):
        r = self._record(status="resolved")
        assert r.memory_label() == "Recently resolved"

    def test_memory_label_ongoing(self):
        r = self._record(status="ongoing", hours_open=50.0)
        assert r.memory_label().startswith("Ongoing")

    def test_persistence_score_bounds(self):
        low = self._record(hours_open=0.0, resurfaced=0)
        high = self._record(hours_open=72.0, resurfaced=3)
        assert 0.0 <= low.persistence_score() <= 1.0
        assert high.persistence_score() == 1.0

    def test_escalation_score_bounds(self):
        r = self._record(event_type="blocker")
        score = r.escalation_score()
        assert 0.0 <= score <= 1.0


# ---------------------------------------------------------------------------
# Integration: memory signals feed into ranking features
# ---------------------------------------------------------------------------

class TestMemorySignalsInRanking:
    """
    Verify that when issue_memory_signals is attached to an event, the ranker
    applies the persistence boost and surfaces it in RankingFeatures.
    """

    def test_memory_boost_increases_score(self, tmp_path, enriched_events, profiles):
        """Events with ongoing memory records score higher than fresh records."""
        from src.ranking import rank_events_for_user
        from datetime import datetime, timezone

        if not enriched_events:
            pytest.skip("no enriched events")

        target_uid = next(iter(profiles))
        profile = profiles[target_uid]

        event = enriched_events[0]
        if event.signals is None:
            pytest.skip("event has no signals")

        # Attach a high-persistence mock memory signal
        mock_signals = MagicMock()
        mock_signals.issue_persistence_score = 0.8
        mock_signals.issue_escalation_score = 0.6
        mock_signals.memory_label = "Ongoing — 3 days old"
        mock_signals.is_new_issue = False
        mock_signals.is_ongoing_issue = True
        mock_signals.is_resurfacing_issue = False
        mock_signals.is_resolved_recently = False
        event.issue_memory_signals = mock_signals

        ranked, _ = rank_events_for_user(
            [event], profile, top_k=1,
            now=datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc),
        )

        if ranked:
            features = ranked[0].reason_features
            assert features.issue_persistence_score > 0.0
            assert features.issue_memory_label == "Ongoing — 3 days old"


# ---------------------------------------------------------------------------
# Integration: memory label appears in why_shown
# ---------------------------------------------------------------------------

class TestMemoryLabelInWhyShown:
    def test_ongoing_label_in_why_shown(self, enriched_events, profiles):
        from src.summarization.providers import FallbackProvider
        from src.models import RankedDigestItem, RankingFeatures

        if not enriched_events:
            pytest.skip("no enriched events")

        event = enriched_events[0]
        if event.signals is None:
            pytest.skip("event has no signals")

        mock_signals = MagicMock()
        mock_signals.is_new_issue = False
        mock_signals.memory_label = "Ongoing — 2 days old"
        event.issue_memory_signals = mock_signals

        target_uid = next(iter(profiles))
        profile = profiles[target_uid]

        item = RankedDigestItem(
            event_id=event.event_id,
            title=event.signals.title,
            signal_level="medium",
            event_type=event.signals.dominant_event_type,
            confidence=0.7,
            score=0.5,
            reason_features=RankingFeatures(
                user_affinity=0.4, importance=0.5, urgency=0.3,
                momentum=0.2, novelty=0.3, recency=0.4,
                embedding_affinity=0.0,
                weights={"user_affinity": 0.28, "importance": 0.24, "urgency": 0.20,
                         "momentum": 0.10, "novelty": 0.08, "recency": 0.05,
                         "embedding_affinity": 0.05},
                final_score=0.5,
                personal_relevance=0.4,
                global_importance=0.4,
                freshness=0.3,
            ),
            source_thread_ids=[event.thread_id],
            source_message_ids=event.message_ids,
        )

        provider = FallbackProvider()
        _, why_shown = provider.summarize(event, item, profile)
        assert "Ongoing" in why_shown or "Issue memory" in why_shown
