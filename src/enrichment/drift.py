"""
Drift and process-debt detection.

Reads IssueMemorySignals already attached to a CandidateEvent and produces
a DriftSignals object describing any recurring-issue or process-debt patterns.

Design principles:
- All field access on issue_memory_signals uses getattr() with safe defaults,
  because the field is typed Optional[Any] on CandidateEvent.
- No LLM calls — purely deterministic, signal-driven.
- Returns safe all-False defaults when issue_memory_signals is None (new issue).
"""

from __future__ import annotations

from .drift_models import DriftSignals


# Hours threshold beyond which an issue is considered "long open"
LONG_OPEN_HOURS_THRESHOLD = 48.0

# Minimum resurfacing count to flag "repeated without resolution"
REPEATED_RESURFACING_THRESHOLD = 2


def detect_drift(event) -> DriftSignals:
    """
    Detect drift and process-debt signals for a CandidateEvent.

    Args:
        event: A CandidateEvent instance (not imported to avoid circular deps).
               Must have .issue_memory_signals and .signals attributes.

    Returns:
        DriftSignals with all flags and scores computed from the issue memory.
        Returns all-default (no drift) if issue_memory_signals is None.
    """
    mem = getattr(event, "issue_memory_signals", None)

    if mem is None:
        return DriftSignals()

    # --- Extract fields from IssueMemorySignals (defensive) ---
    resurfacing_count: int = getattr(mem, "resurfaced_count", 0) or 0
    hours_open: float = getattr(mem, "issue_age_hours", 0.0) or 0.0
    last_event_type: str = ""

    # IssueMemorySignals does not expose last_event_type directly,
    # but the underlying IssueRecord does. We try both.
    raw_record = getattr(mem, "_record", None)
    if raw_record is not None:
        last_event_type = getattr(raw_record, "last_event_type", "") or ""

    # Also check if the matcher attached it directly on the signals object
    # (some implementations expose it as a flat attribute)
    if not last_event_type:
        last_event_type = getattr(mem, "last_event_type", "") or ""

    # Current dominant event type from SemanticSignals
    signals = getattr(event, "signals", None)
    current_type: str = ""
    if signals is not None:
        current_type = getattr(signals, "dominant_event_type", "") or ""

    # --- Detection rules ---

    repeated_without_resolution = resurfacing_count >= REPEATED_RESURFACING_THRESHOLD

    recurring_blocker_flag = (
        repeated_without_resolution and current_type == "blocker"
    )

    stale_mitigation_flag = (
        last_event_type == "decision"
        and current_type in ("blocker", "risk")
    )

    long_open_flag = hours_open > LONG_OPEN_HOURS_THRESHOLD

    # drift_flag: True if any meaningful drift condition fires
    # Note: long_open alone only fires if the issue has also resurfaced at least once
    # (long open without any recurrence is just a slow burn, not necessarily drift)
    drift_flag = (
        repeated_without_resolution
        or stale_mitigation_flag
        or recurring_blocker_flag
        or (long_open_flag and resurfacing_count >= 1)
    )

    # --- Process-debt score ---
    score = 0.0
    if repeated_without_resolution:
        score += 0.25
    if recurring_blocker_flag:
        score += 0.20
    if stale_mitigation_flag:
        score += 0.25
    if long_open_flag:
        score += 0.15
    score += min(0.15, resurfacing_count * 0.05)
    score = min(1.0, score)

    # --- Human-readable drift reason ---
    reasons: list[str] = []
    if recurring_blocker_flag:
        reasons.append(f"Recurring blocker — resurfaced {resurfacing_count} times without resolution")
    elif repeated_without_resolution:
        reasons.append(f"Resurfaced {resurfacing_count} times without resolution")
    if stale_mitigation_flag:
        reasons.append("Previous decision appears insufficient — problem recurred")
    if long_open_flag and resurfacing_count >= 1:
        days = hours_open / 24.0
        reasons.append(f"Long-running issue with renewed activity ({days:.1f} days open)")
    elif long_open_flag:
        days = hours_open / 24.0
        reasons.append(f"Long-running issue — active for {days:.1f} days")

    drift_reason = "; ".join(reasons) if reasons else ""

    # --- Process-debt label ---
    if recurring_blocker_flag:
        label = f"Recurring blocker — {resurfacing_count} resurfacing(s)"
    elif stale_mitigation_flag:
        label = "Stale mitigation — problem recurred after decision"
    elif long_open_flag and drift_flag:
        label = f"Long-open issue — {hours_open:.0f}h open"
    elif drift_flag:
        label = "Possible process debt"
    else:
        label = ""

    return DriftSignals(
        drift_flag=drift_flag,
        drift_reason=drift_reason,
        resurfacing_count=resurfacing_count,
        repeated_without_resolution=repeated_without_resolution,
        stale_mitigation_flag=stale_mitigation_flag,
        recurring_blocker_flag=recurring_blocker_flag,
        long_open_flag=long_open_flag,
        process_debt_score=round(score, 4),
        process_debt_label=label,
    )
