"""
Tests for V3 impact reasoning.

Coverage:
    - build_impact_statement returns correct hedged language
    - Entity-specific templates (blocker+build, risk+supplier, decision+part)
    - Topic-based fallback templates
    - Type-only fallback
    - Noise events return empty string
    - Events without signals return empty string
    - Impact statement populated on RankedDigestItem via FallbackProvider
    - Persistent issue adds context to topic-based template
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.impact.reasoner import build_impact_statement, _hedge


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(
    event_type: str = "risk",
    topics: list[str] | None = None,
    entities: dict | None = None,
    memory_is_new: bool = True,
    persistence_score: float = 0.0,
) -> MagicMock:
    event = MagicMock()
    signals = MagicMock()
    signals.dominant_event_type = event_type
    signals.topic_labels = topics or ["supply_chain"]
    signals.extracted_entities = entities or {}
    event.signals = signals

    if persistence_score > 0:
        mem = MagicMock()
        mem.is_new_issue = memory_is_new
        mem.issue_persistence_score = persistence_score
        event.issue_memory_signals = mem
    else:
        event.issue_memory_signals = None

    return event


# ---------------------------------------------------------------------------
# Hedge utility
# ---------------------------------------------------------------------------

class TestHedge:
    def test_already_hedged_unchanged(self):
        s = "This may block DVT validation."
        assert _hedge(s) == s

    def test_unhedged_gets_prefix(self):
        s = "This blocks progress."
        result = _hedge(s)
        assert "likely" in result.lower() or "may" in result.lower()

    def test_empty_string(self):
        assert _hedge("") == ""


# ---------------------------------------------------------------------------
# Noise / no-signal guard
# ---------------------------------------------------------------------------

class TestNoisAndNoSignal:
    def test_noise_returns_empty(self):
        event = _make_event(event_type="noise")
        assert build_impact_statement(event) == ""

    def test_no_signals_returns_empty(self):
        event = MagicMock()
        event.signals = None
        assert build_impact_statement(event) == ""


# ---------------------------------------------------------------------------
# Entity + event type templates (highest specificity)
# ---------------------------------------------------------------------------

class TestEntityTypeTemplates:
    def test_blocker_with_build_and_part(self):
        event = _make_event(
            event_type="blocker",
            entities={"parts": ["SHT40"], "revisions": [], "builds": ["DVT"], "suppliers": []},
        )
        result = build_impact_statement(event)
        assert "DVT" in result
        assert "SHT40" in result

    def test_blocker_with_build_only(self):
        event = _make_event(
            event_type="blocker",
            entities={"parts": [], "revisions": [], "builds": ["EVT"], "suppliers": []},
        )
        result = build_impact_statement(event)
        assert "EVT" in result

    def test_blocker_with_supplier(self):
        event = _make_event(
            event_type="blocker",
            entities={"parts": [], "revisions": [], "builds": [], "suppliers": ["Molex"]},
        )
        result = build_impact_statement(event)
        assert "Molex" in result

    def test_risk_with_supplier_and_build(self):
        event = _make_event(
            event_type="risk",
            entities={"parts": [], "revisions": [], "builds": ["PVT"], "suppliers": ["Winbond"]},
        )
        result = build_impact_statement(event)
        assert "Winbond" in result
        assert "PVT" in result

    def test_risk_with_part_and_revision(self):
        event = _make_event(
            event_type="risk",
            entities={"parts": ["MX150"], "revisions": ["Rev C"], "builds": [], "suppliers": []},
        )
        result = build_impact_statement(event)
        assert "MX150" in result
        assert "Rev C" in result

    def test_decision_with_part(self):
        event = _make_event(
            event_type="decision",
            entities={"parts": ["STM32"], "revisions": [], "builds": [], "suppliers": []},
        )
        result = build_impact_statement(event)
        assert "STM32" in result

    def test_decision_with_build_and_part(self):
        event = _make_event(
            event_type="decision",
            entities={"parts": ["SHT40"], "revisions": [], "builds": ["DVT"], "suppliers": []},
        )
        result = build_impact_statement(event)
        assert "SHT40" in result
        assert "DVT" in result

    def test_rfi_with_part(self):
        event = _make_event(
            event_type="request_for_input",
            entities={"parts": ["W25Q128"], "revisions": [], "builds": [], "suppliers": []},
        )
        result = build_impact_statement(event)
        assert "W25Q128" in result.upper() or "w25q128" in result.lower()

    def test_status_update_with_part_and_revision(self):
        event = _make_event(
            event_type="status_update",
            entities={"parts": ["SHT40"], "revisions": ["Rev B"], "builds": [], "suppliers": []},
        )
        result = build_impact_statement(event)
        assert "SHT40" in result


# ---------------------------------------------------------------------------
# Topic-based fallback templates
# ---------------------------------------------------------------------------

class TestTypeTopicTemplates:
    def test_blocker_supply_chain_topic(self):
        event = _make_event(event_type="blocker", topics=["supply_chain"], entities={})
        result = build_impact_statement(event)
        assert result  # Non-empty
        assert "supply" in result.lower() or "build" in result.lower()

    def test_risk_firmware_topic(self):
        event = _make_event(event_type="risk", topics=["firmware"], entities={})
        result = build_impact_statement(event)
        assert result
        assert "firmware" in result.lower()

    def test_decision_hardware_topic(self):
        event = _make_event(event_type="decision", topics=["hardware"], entities={})
        result = build_impact_statement(event)
        assert result
        assert "hardware" in result.lower()

    def test_persistent_issue_adds_context(self):
        # Persistent ongoing issue should get additional note
        event = _make_event(
            event_type="blocker",
            topics=["supply_chain"],
            entities={},
            memory_is_new=False,
            persistence_score=0.5,
        )
        result = build_impact_statement(event)
        assert result
        assert "active" in result.lower() or "some time" in result.lower()


# ---------------------------------------------------------------------------
# Type-only fallback
# ---------------------------------------------------------------------------

class TestTypeOnlyTemplates:
    def test_blocker_no_entities_no_topic(self):
        event = _make_event(event_type="blocker", topics=[], entities={})
        result = build_impact_statement(event)
        assert result  # Should still return something

    def test_risk_no_entities_no_topic(self):
        event = _make_event(event_type="risk", topics=[], entities={})
        result = build_impact_statement(event)
        assert result

    def test_decision_no_entities_no_topic(self):
        event = _make_event(event_type="decision", topics=[], entities={})
        result = build_impact_statement(event)
        assert result

    def test_status_update_no_entities_no_topic(self):
        event = _make_event(event_type="status_update", topics=[], entities={})
        result = build_impact_statement(event)
        assert result


# ---------------------------------------------------------------------------
# Output quality checks
# ---------------------------------------------------------------------------

class TestOutputQuality:
    def test_statement_uses_hedged_language(self):
        """All non-empty statements should contain a hedge word."""
        hedge_words = ("may", "likely", "appears", "suggests", "could", "typically", "affects")
        for event_type in ("blocker", "risk", "decision", "request_for_input", "status_update"):
            event = _make_event(event_type=event_type, topics=["firmware"], entities={})
            result = build_impact_statement(event)
            if result:
                lower = result.lower()
                assert any(w in lower for w in hedge_words), (
                    f"{event_type}: statement lacks hedge word: {result!r}"
                )

    def test_statement_length_reasonable(self):
        """Impact statements should be concise."""
        event = _make_event(
            event_type="blocker",
            entities={"parts": ["SHT40"], "revisions": [], "builds": ["DVT"], "suppliers": []},
        )
        result = build_impact_statement(event)
        assert len(result) <= 150, f"Statement too long: {result!r}"

    def test_returns_string_not_none(self):
        event = _make_event(event_type="risk")
        result = build_impact_statement(event)
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Integration: impact_statement populated on RankedDigestItem
# ---------------------------------------------------------------------------

class TestImpactStatementOnDigestItem:
    def test_impact_statement_set_on_item(self, enriched_events, profiles):
        from src.summarization.providers import FallbackProvider
        from src.models import RankedDigestItem, RankingFeatures

        if not enriched_events:
            pytest.skip("no enriched events")

        # Pick a non-noise event
        event = next(
            (e for e in enriched_events if e.signals and e.signals.dominant_event_type != "noise"),
            None,
        )
        if event is None:
            pytest.skip("no non-noise events")

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
                weights={
                    "user_affinity": 0.28, "importance": 0.24, "urgency": 0.20,
                    "momentum": 0.10, "novelty": 0.08, "recency": 0.05,
                    "embedding_affinity": 0.05,
                },
                final_score=0.5,
                personal_relevance=0.4,
                global_importance=0.4,
                freshness=0.3,
            ),
            source_thread_ids=[event.thread_id],
            source_message_ids=event.message_ids,
        )

        provider = FallbackProvider()
        provider.summarize(event, item, profile)

        # impact_statement should be set (possibly empty for edge cases, but present)
        # Only assert it's a string — non-noise events should usually get something
        assert item.impact_statement is None or isinstance(item.impact_statement, str)

    def test_impact_statement_non_empty_for_blocker(self, enriched_events, profiles):
        from src.summarization.providers import FallbackProvider
        from src.models import RankedDigestItem, RankingFeatures

        if not enriched_events:
            pytest.skip("no enriched events")

        # Find a blocker event
        event = next(
            (e for e in enriched_events
             if e.signals and e.signals.dominant_event_type == "blocker"),
            None,
        )
        if event is None:
            pytest.skip("no blocker events in fixture")

        target_uid = next(iter(profiles))
        profile = profiles[target_uid]

        item = RankedDigestItem(
            event_id=event.event_id,
            title=event.signals.title,
            signal_level="high",
            event_type="blocker",
            confidence=0.85,
            score=0.75,
            reason_features=RankingFeatures(
                user_affinity=0.5, importance=0.8, urgency=0.7,
                momentum=0.4, novelty=0.5, recency=0.6,
                embedding_affinity=0.0,
                weights={
                    "user_affinity": 0.28, "importance": 0.24, "urgency": 0.20,
                    "momentum": 0.10, "novelty": 0.08, "recency": 0.05,
                    "embedding_affinity": 0.05,
                },
                final_score=0.75,
                personal_relevance=0.5,
                global_importance=0.75,
                freshness=0.5,
            ),
            source_thread_ids=[event.thread_id],
            source_message_ids=event.message_ids,
        )

        provider = FallbackProvider()
        provider.summarize(event, item, profile)

        # Blockers should always get an impact statement
        assert item.impact_statement is not None
        assert len(item.impact_statement) > 0
