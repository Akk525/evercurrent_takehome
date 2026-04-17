"""
Issue memory matching — map current event clusters to persistent issue IDs.

Matching strategy:
    1. Entity fingerprint Jaccard similarity (primary signal)
       - High-value entities (parts, revisions, builds, suppliers) only
       - Threshold: Jaccard ≥ 0.25 → candidate match
    2. Dominant topic match (tiebreaker / boost)
       - Same dominant topic adds confidence
    3. If no match found → new issue

The match threshold is intentionally loose (0.25) because:
    - Entity sets change over time as new details emerge
    - Partial overlap is still meaningful (e.g. same part number, different build)
    - The primary goal is identity continuity, not strict deduplication

Status transitions:
    - new      → the issue was never seen before this run
    - ongoing  → seen in a prior run within 48h
    - resurfacing → seen before, but a quiet gap > 24h elapsed
    - resolved → marked by a state_change_hint containing "resolved"

Escalation detection:
    - If the new event type is more severe than the previous type, escalate
    - Severity: noise < status_update < request_for_input < decision < risk < blocker
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from dataclasses import dataclass

from .store import (
    IssueMemoryStore,
    IssueRecord,
    make_entity_fingerprint,
    new_issue_record,
)

# Matching thresholds
ENTITY_JACCARD_THRESHOLD = 0.25   # Min Jaccard similarity to consider a match
QUIET_GAP_HOURS = 24.0            # Hours of inactivity before "resurfacing" label
ONGOING_WINDOW_HOURS = 48.0       # Max gap to still be considered "ongoing"

# Severity ordering for escalation detection
_TYPE_SEVERITY: dict[str, int] = {
    "noise": 0,
    "status_update": 1,
    "request_for_input": 2,
    "decision": 3,
    "risk": 4,
    "blocker": 5,
}


@dataclass
class IssueMemorySignals:
    """
    Per-event issue-memory signals, exposed for ranking and digest wording.
    Attached to CandidateEvent after matching.
    """
    persistent_issue_id: str
    is_new_issue: bool
    is_ongoing_issue: bool
    is_resurfacing_issue: bool
    is_resolved_recently: bool
    issue_age_hours: float
    resurfaced_count: int
    escalation_count: int
    issue_persistence_score: float   # [0, 1] — how long-running
    issue_escalation_score: float    # [0, 1] — how severe / escalated
    memory_label: str                # "Ongoing for 2 days", "Resurfaced", etc.
    age_label: str                   # "new today", "3 days old"


def match_and_update_issues(
    events: list,         # list[CandidateEvent]
    store: IssueMemoryStore,
    now: datetime,
) -> None:
    """
    Match current events to persistent issues, create new records for unmatched
    events, and annotate each event with `issue_memory_signals`.

    Mutates events in-place (sets event.issue_memory_signals).
    Writes updated/new records to the SQLite store.

    Design: operates at the event level (not cluster level) because the
    ephemeral cluster IDs are not stable across runs. Each event independently
    matches to a persistent issue.
    """
    existing_issues = store.load_all()

    for event in events:
        if event.signals is None:
            continue

        # Build current event's entity fingerprint
        entity_fp = make_entity_fingerprint(event.signals.extracted_entities)
        current_entities = set(e for e in entity_fp.split("|") if e)
        dominant_topic = (
            event.signals.topic_labels[0] if event.signals.topic_labels else ""
        )
        dominant_type = event.signals.dominant_event_type
        title = event.signals.title

        # Find best matching existing issue
        best_match: IssueRecord | None = None
        best_score = 0.0

        for issue in existing_issues:
            score = _match_score(current_entities, issue, dominant_topic)
            if score > best_score and score >= ENTITY_JACCARD_THRESHOLD:
                best_score = score
                best_match = issue

        if best_match is not None:
            # Update existing issue
            updated = _update_issue(best_match, event, entity_fp, now)
            store.upsert(updated)
            signals = _signals_from_record(updated)
        else:
            # Create new issue
            thread_ids = [event.thread_id]
            record = new_issue_record(
                event_id=event.event_id,
                thread_ids=thread_ids,
                title=title,
                event_type=dominant_type,
                dominant_topic=dominant_topic,
                entity_fingerprint=entity_fp,
                now=now,
            )
            store.upsert(record)
            # Also add to existing_issues so later events in this run can match it
            existing_issues.append(record)
            signals = _signals_from_record(record)

        # Attach signals to event (declared as Optional[Any] on CandidateEvent, excluded from serialization)
        event.issue_memory_signals = signals


def _match_score(
    current_entities: set[str],
    issue: IssueRecord,
    current_topic: str,
) -> float:
    """
    Compute a match score between a current event and a stored issue.

    Primary: entity Jaccard similarity
    Boost: +0.1 if dominant topics match
    """
    issue_entities = issue.entity_set()

    if not current_entities and not issue_entities:
        # Both empty — topic-only match
        if current_topic and current_topic == issue.dominant_topic:
            return 0.3
        return 0.0

    if not current_entities or not issue_entities:
        return 0.0

    intersection = len(current_entities & issue_entities)
    union = len(current_entities | issue_entities)
    jaccard = intersection / union if union > 0 else 0.0

    topic_boost = 0.1 if (current_topic and current_topic == issue.dominant_topic) else 0.0

    return round(min(jaccard + topic_boost, 1.0), 3)


def _update_issue(
    existing: IssueRecord,
    event,          # CandidateEvent
    new_entity_fp: str,
    now: datetime,
) -> IssueRecord:
    """
    Update an existing issue record with data from a newly matched event.

    Determines new status, escalation, hours_open, resurfaced_count.
    """
    first_seen_dt = datetime.fromisoformat(existing.first_seen)
    last_seen_dt = datetime.fromisoformat(existing.last_seen)

    hours_open = (now - first_seen_dt).total_seconds() / 3600.0
    hours_since_last = (now - last_seen_dt).total_seconds() / 3600.0

    # Status transition
    prior_status = existing.current_status
    if hours_since_last > ONGOING_WINDOW_HOURS:
        new_status = "resurfacing"
        resurfaced_count = existing.resurfaced_count + 1
    else:
        new_status = "ongoing"
        resurfaced_count = existing.resurfaced_count

    # Check if resolved (state_change_hint from signals)
    if event.signals and event.signals.state_change_hint:
        hint = event.signals.state_change_hint.lower()
        if "resolved" in hint or "decision made" in hint:
            new_status = "resolved"

    # Escalation detection
    prior_severity = _TYPE_SEVERITY.get(existing.last_event_type, 0)
    current_severity = _TYPE_SEVERITY.get(
        event.signals.dominant_event_type if event.signals else "noise", 0
    )
    escalation_count = existing.escalation_count
    if current_severity > prior_severity:
        escalation_count += 1

    # Merge entity fingerprints (union — accumulate known entities)
    existing_entities = set(e for e in existing.entity_fingerprint.split("|") if e)
    new_entities = set(e for e in new_entity_fp.split("|") if e)
    merged_fp = "|".join(sorted(existing_entities | new_entities))

    # Update thread IDs
    existing_threads = set(existing.thread_ids())
    existing_threads.add(event.thread_id)

    return IssueRecord(
        issue_id=existing.issue_id,
        first_seen=existing.first_seen,
        last_seen=now.isoformat(),
        current_status=new_status,
        prior_status=prior_status,
        hours_open=round(hours_open, 2),
        resurfaced_count=resurfaced_count,
        escalation_count=escalation_count,
        dominant_topic=existing.dominant_topic or (
            event.signals.topic_labels[0] if event.signals and event.signals.topic_labels else ""
        ),
        entity_fingerprint=merged_fp,
        related_thread_ids=json.dumps(sorted(existing_threads)),
        last_event_id=event.event_id,
        last_title=event.signals.title if event.signals else existing.last_title,
        last_event_type=event.signals.dominant_event_type if event.signals else existing.last_event_type,
        updated_at=now.isoformat(),
    )


def _signals_from_record(record: IssueRecord) -> IssueMemorySignals:
    """Build IssueMemorySignals from a persisted IssueRecord."""
    return IssueMemorySignals(
        persistent_issue_id=record.issue_id,
        is_new_issue=record.current_status == "new",
        is_ongoing_issue=record.current_status == "ongoing",
        is_resurfacing_issue=record.current_status == "resurfacing",
        is_resolved_recently=record.current_status == "resolved",
        issue_age_hours=record.hours_open,
        resurfaced_count=record.resurfaced_count,
        escalation_count=record.escalation_count,
        issue_persistence_score=record.persistence_score(),
        issue_escalation_score=record.escalation_score(),
        memory_label=record.memory_label(),
        age_label=record.age_label(),
    )
