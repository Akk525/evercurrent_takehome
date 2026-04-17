"""
Tests for drift / process-debt detection (src/enrichment/drift.py).

Fixtures are built by hand — no pipeline execution required.
IssueMemorySignals is reproduced as a lightweight dataclass-like object
to match what the matcher attaches at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pytest

from src.enrichment.drift import detect_drift
from src.enrichment.drift_models import DriftSignals


# ---------------------------------------------------------------------------
# Minimal stubs — match the fields actually accessed in detect_drift
# ---------------------------------------------------------------------------

@dataclass
class _FakeMemorySignals:
    """Mimics IssueMemorySignals from src/issue_memory/matcher.py."""
    persistent_issue_id: str = "issue-001"
    is_new_issue: bool = False
    is_ongoing_issue: bool = True
    is_resurfacing_issue: bool = False
    is_resolved_recently: bool = False
    issue_age_hours: float = 0.0
    resurfaced_count: int = 0
    escalation_count: int = 0
    issue_persistence_score: float = 0.0
    issue_escalation_score: float = 0.0
    memory_label: str = "Ongoing"
    age_label: str = "new today"
    # last_event_type is on IssueRecord, but some paths expose it here too
    last_event_type: str = ""


@dataclass
class _FakeSemanticSignals:
    """Mimics SemanticSignals from src/models/derived.py."""
    dominant_event_type: str = "blocker"
    title: str = "Test event"
    topic_labels: list = field(default_factory=list)
    state_change_hint: Optional[str] = None


@dataclass
class _FakeEvent:
    """Minimal stand-in for CandidateEvent."""
    event_id: str = "evt-001"
    thread_id: str = "thread-001"
    issue_memory_signals: Optional[_FakeMemorySignals] = None
    signals: Optional[_FakeSemanticSignals] = None


# ---------------------------------------------------------------------------
# Helper factory — keeps test bodies focused on the variable under test
# ---------------------------------------------------------------------------

def _make_event(
    resurfaced_count: int = 0,
    hours_open: float = 0.0,
    last_event_type: str = "",
    dominant_event_type: str = "blocker",
    has_memory: bool = True,
) -> _FakeEvent:
    mem = (
        _FakeMemorySignals(
            resurfaced_count=resurfaced_count,
            issue_age_hours=hours_open,
            last_event_type=last_event_type,
        )
        if has_memory
        else None
    )
    signals = _FakeSemanticSignals(dominant_event_type=dominant_event_type)
    return _FakeEvent(issue_memory_signals=mem, signals=signals)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestNoDrift:
    def test_no_memory_signals_returns_all_defaults(self):
        """Event with no issue_memory_signals → safe defaults, drift_flag=False."""
        event = _make_event(has_memory=False)
        result = detect_drift(event)

        assert isinstance(result, DriftSignals)
        assert result.drift_flag is False
        assert result.resurfacing_count == 0
        assert result.repeated_without_resolution is False
        assert result.stale_mitigation_flag is False
        assert result.recurring_blocker_flag is False
        assert result.long_open_flag is False
        assert result.process_debt_score == 0.0
        assert result.process_debt_label == ""

    def test_single_resurfacing_not_repeated(self):
        """resurfacing_count=1 → repeated_without_resolution must be False."""
        event = _make_event(resurfaced_count=1, hours_open=10.0)
        result = detect_drift(event)

        assert result.repeated_without_resolution is False
        # long_open_flag is False (10h < 48h), resurfaced_count=1 < 2
        # drift_flag should be False
        assert result.drift_flag is False

    def test_short_open_no_resurfacing_no_drift(self):
        """Brand-new issue, hours_open=5, resurfaced_count=0 → no drift."""
        event = _make_event(resurfaced_count=0, hours_open=5.0)
        result = detect_drift(event)

        assert result.drift_flag is False
        assert result.process_debt_score == 0.0


class TestRepeatedWithoutResolution:
    def test_two_resurfacings_flags_repeated(self):
        """resurfacing_count=2 → repeated_without_resolution=True, drift_flag=True."""
        event = _make_event(resurfaced_count=2, hours_open=10.0)
        result = detect_drift(event)

        assert result.repeated_without_resolution is True
        assert result.drift_flag is True
        assert result.resurfacing_count == 2

    def test_three_resurfacings_still_flags(self):
        """resurfacing_count=3 → repeated_without_resolution=True."""
        event = _make_event(resurfaced_count=3, hours_open=20.0)
        result = detect_drift(event)

        assert result.repeated_without_resolution is True
        assert result.drift_flag is True


class TestRecurringBlocker:
    def test_blocker_with_two_resurfacings(self):
        """dominant_event_type=blocker AND resurfaced_count>=2 → recurring_blocker_flag=True."""
        event = _make_event(
            resurfaced_count=2,
            hours_open=15.0,
            dominant_event_type="blocker",
        )
        result = detect_drift(event)

        assert result.recurring_blocker_flag is True
        assert result.drift_flag is True

    def test_non_blocker_with_two_resurfacings_no_recurring_blocker(self):
        """dominant_event_type=status_update AND resurfaced_count=2 → recurring_blocker_flag=False."""
        event = _make_event(
            resurfaced_count=2,
            hours_open=15.0,
            dominant_event_type="status_update",
        )
        result = detect_drift(event)

        assert result.recurring_blocker_flag is False
        # But repeated_without_resolution should still be True
        assert result.repeated_without_resolution is True


class TestStaleMitigation:
    def test_previous_decision_current_blocker(self):
        """last_event_type=decision, current=blocker → stale_mitigation_flag=True."""
        event = _make_event(
            resurfaced_count=1,
            hours_open=20.0,
            last_event_type="decision",
            dominant_event_type="blocker",
        )
        result = detect_drift(event)

        assert result.stale_mitigation_flag is True
        assert result.drift_flag is True

    def test_previous_decision_current_risk(self):
        """last_event_type=decision, current=risk → stale_mitigation_flag=True."""
        event = _make_event(
            resurfaced_count=0,
            hours_open=10.0,
            last_event_type="decision",
            dominant_event_type="risk",
        )
        result = detect_drift(event)

        assert result.stale_mitigation_flag is True
        assert result.drift_flag is True

    def test_previous_decision_current_status_update_no_flag(self):
        """last_event_type=decision, current=status_update → stale_mitigation_flag=False."""
        event = _make_event(
            resurfaced_count=0,
            hours_open=10.0,
            last_event_type="decision",
            dominant_event_type="status_update",
        )
        result = detect_drift(event)

        assert result.stale_mitigation_flag is False


class TestLongOpen:
    def test_long_open_with_resurfacing_triggers_drift(self):
        """hours_open>48 AND resurfacing_count>=1 → long_open_flag=True, drift_flag=True."""
        event = _make_event(resurfaced_count=1, hours_open=72.0)
        result = detect_drift(event)

        assert result.long_open_flag is True
        assert result.drift_flag is True

    def test_long_open_without_resurfacing_no_drift(self):
        """hours_open>48 but resurfaced_count=0 → long_open_flag=True, drift_flag=False."""
        event = _make_event(resurfaced_count=0, hours_open=72.0)
        result = detect_drift(event)

        assert result.long_open_flag is True
        # long_open alone without resurfacing does not trigger drift
        assert result.drift_flag is False

    def test_just_below_threshold_no_long_open(self):
        """hours_open=48.0 (not strictly greater) → long_open_flag=False."""
        event = _make_event(resurfaced_count=0, hours_open=48.0)
        result = detect_drift(event)

        assert result.long_open_flag is False


class TestProcessDebtScore:
    def test_score_in_range(self):
        """process_debt_score is always in [0, 1]."""
        # Maximally bad event: recurring blocker, stale mitigation, long open, many resurfacings
        event = _make_event(
            resurfaced_count=10,
            hours_open=200.0,
            last_event_type="decision",
            dominant_event_type="blocker",
        )
        result = detect_drift(event)

        assert 0.0 <= result.process_debt_score <= 1.0

    def test_no_drift_score_is_zero(self):
        """Event with no memory signals → score = 0.0."""
        event = _make_event(has_memory=False)
        result = detect_drift(event)

        assert result.process_debt_score == 0.0

    def test_score_increases_with_severity(self):
        """More drift conditions active → higher score."""
        mild_event = _make_event(resurfaced_count=2, hours_open=10.0)
        severe_event = _make_event(
            resurfaced_count=4,
            hours_open=120.0,
            last_event_type="decision",
            dominant_event_type="blocker",
        )
        mild_result = detect_drift(mild_event)
        severe_result = detect_drift(severe_event)

        assert severe_result.process_debt_score > mild_result.process_debt_score


class TestProcessDebtLabel:
    def test_label_non_empty_when_drift(self):
        """process_debt_label is a non-empty string when drift_flag=True."""
        event = _make_event(resurfaced_count=2, hours_open=10.0)
        result = detect_drift(event)

        assert result.drift_flag is True
        assert isinstance(result.process_debt_label, str)
        assert len(result.process_debt_label) > 0

    def test_recurring_blocker_label_content(self):
        """Recurring blocker label contains 'blocker' and resurfacing count."""
        event = _make_event(
            resurfaced_count=3,
            hours_open=20.0,
            dominant_event_type="blocker",
        )
        result = detect_drift(event)

        assert result.recurring_blocker_flag is True
        assert "blocker" in result.process_debt_label.lower()
        assert "3" in result.process_debt_label

    def test_stale_mitigation_label_content(self):
        """Stale mitigation label contains 'decision' or 'mitigation'."""
        event = _make_event(
            resurfaced_count=0,
            hours_open=10.0,
            last_event_type="decision",
            dominant_event_type="risk",
        )
        result = detect_drift(event)

        assert result.stale_mitigation_flag is True
        label_lower = result.process_debt_label.lower()
        assert "mitigation" in label_lower or "decision" in label_lower

    def test_label_empty_when_no_drift(self):
        """process_debt_label is empty string when no drift detected."""
        event = _make_event(has_memory=False)
        result = detect_drift(event)

        assert result.process_debt_label == ""
