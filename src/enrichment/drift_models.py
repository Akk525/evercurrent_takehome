"""
Drift and process-debt detection models.

These signals are computed from issue memory history and indicate whether
an event represents a recurring, unresolved, or escalating problem pattern.

All scores are floats in [0, 1]. All fields default to "no drift detected"
so that events without issue memory history remain unaffected.
"""

from pydantic import BaseModel


class DriftSignals(BaseModel):
    """Detected drift and process-debt signals for a CandidateEvent."""

    # Top-level drift flag: True if any drift condition is active
    drift_flag: bool = False

    # Human-readable explanation of why drift was flagged
    # e.g. "Resurfaced after 3 days quiet", "5th occurrence without resolution"
    drift_reason: str = ""

    # Raw resurfacing count from issue memory
    resurfacing_count: int = 0

    # True if the issue has resurfaced >= 2 times without being resolved
    repeated_without_resolution: bool = False

    # True if the last recorded event type was "decision" but the current event
    # is still a "blocker" or "risk" — the decision did not fix the problem
    stale_mitigation_flag: bool = False

    # True if dominant_event_type is "blocker" AND repeated_without_resolution
    recurring_blocker_flag: bool = False

    # True if the issue has been open for more than 48 hours
    long_open_flag: bool = False

    # Composite process-debt score in [0, 1]
    process_debt_score: float = 0.0

    # Human-readable label summarising the detected debt pattern
    # e.g. "Recurring blocker — 3 resurfacings", "Stale mitigation — problem recurred after decision"
    process_debt_label: str = ""
