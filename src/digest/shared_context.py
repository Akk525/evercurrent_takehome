"""
Shared context and misalignment detection layer.

Operates at the org-wide level — additive to per-user personalized digests.
All inferences are probabilistic; outputs use hedged language by convention.

Public API:
    build_shared_context(events, profiles) -> SharedContextView
    detect_misalignments(events, profiles) -> list[MisalignmentSignal]
"""

from __future__ import annotations

from src.models import CandidateEvent, UserContextProfile

from .shared_context_models import (
    MisalignmentSignal,
    SharedContextItem,
    SharedContextView,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_GLOBALLY_CRITICAL = 5
_MAX_CROSS_FUNCTIONAL_HOTSPOTS = 3

# Thresholds — kept explicit for easy tuning / auditing
_GLOBALLY_CRITICAL_IMPORTANCE_THRESHOLD = 0.65
_GLOBALLY_CRITICAL_URGENCY_THRESHOLD = 0.5
_GLOBALLY_CRITICAL_UNRESOLVED_THRESHOLD = 0.6
_GLOBALLY_CRITICAL_CROSS_FUNCTIONAL_THRESHOLD = 0.5
_GLOBALLY_CRITICAL_CROSS_FUNCTIONAL_IMPORTANCE_THRESHOLD = 0.5

_HOTSPOT_CROSS_FUNCTIONAL_THRESHOLD = 0.6
_HOTSPOT_MIN_PARTICIPANTS = 3

_MISALIGNMENT_IMPORTANCE_THRESHOLD = 0.6
_MISALIGNMENT_CROSS_FUNCTIONAL_THRESHOLD = 0.5
_MISALIGNMENT_MIN_PARTICIPANTS = 3  # "many participants"


# ---------------------------------------------------------------------------
# Shared-context score weights
# ---------------------------------------------------------------------------

_SC_IMPORTANCE_W = 0.4
_SC_CROSS_FUNCTIONAL_W = 0.3
_SC_URGENCY_W = 0.2
_SC_UNRESOLVED_W = 0.1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_signals(event: CandidateEvent) -> dict[str, float]:
    """Return a flat dict of signal values, guarding against None signals."""
    s = event.signals
    if s is None:
        return {
            "importance_score": 0.0,
            "urgency_score": 0.0,
            "unresolved_score": 0.0,
            "cross_functional_score": 0.0,
            "momentum_score": 0.0,
        }
    return {
        "importance_score": getattr(s, "importance_score", 0.0),
        "urgency_score": getattr(s, "urgency_score", 0.0),
        "unresolved_score": getattr(s, "unresolved_score", 0.0),
        "cross_functional_score": getattr(s, "cross_functional_score", 0.0),
        "momentum_score": getattr(s, "momentum_score", 0.0),
    }


def _signal_level(score: float) -> str:
    if score >= 0.65:
        return "high"
    elif score >= 0.40:
        return "medium"
    return "low"


def _compute_shared_context_score(sig: dict[str, float]) -> float:
    """Weighted composite in [0, 1]."""
    raw = (
        _SC_IMPORTANCE_W * sig["importance_score"]
        + _SC_CROSS_FUNCTIONAL_W * sig["cross_functional_score"]
        + _SC_URGENCY_W * sig["urgency_score"]
        + _SC_UNRESOLVED_W * sig["unresolved_score"]
    )
    return round(min(max(raw, 0.0), 1.0), 4)


def _compose_reason(sig: dict[str, float], event_type: str) -> str:
    """
    Build a hedged reason string for a SharedContextItem.
    Describes the most prominent signals without overclaiming.
    """
    parts = []

    if sig["importance_score"] >= _GLOBALLY_CRITICAL_IMPORTANCE_THRESHOLD:
        parts.append("high-importance")

    if event_type in ("blocker", "risk"):
        parts.append(f"likely {event_type}")
    elif event_type == "decision":
        parts.append("pending decision")

    if sig["urgency_score"] >= _GLOBALLY_CRITICAL_URGENCY_THRESHOLD:
        parts.append("appears time-sensitive")

    if sig["unresolved_score"] >= _GLOBALLY_CRITICAL_UNRESOLVED_THRESHOLD:
        parts.append("appears unresolved")

    if sig["cross_functional_score"] >= _GLOBALLY_CRITICAL_CROSS_FUNCTIONAL_THRESHOLD:
        parts.append("spans multiple teams")

    if not parts:
        parts.append("elevated signal across multiple dimensions")

    return "; ".join(parts).capitalize() + "."


def _event_type(event: CandidateEvent) -> str:
    if event.signals is None:
        return "unknown"
    return getattr(event.signals, "dominant_event_type", "unknown")


def _event_title(event: CandidateEvent) -> str:
    if event.signals is None:
        return event.event_id
    return getattr(event.signals, "title", event.event_id)


def _topic_labels(event: CandidateEvent) -> list[str]:
    if event.signals is None:
        return []
    return list(getattr(event.signals, "topic_labels", []))


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

def build_shared_context(
    events: list[CandidateEvent],
    profiles: dict[str, UserContextProfile],
    generated_for_user_id: str | None = None,
) -> SharedContextView:
    """
    Identify globally critical events and cross-functional hotspots.

    This is an additive layer — it does not modify or replace per-user digests.
    """
    globally_critical = _select_globally_critical(events)
    cross_functional_hotspots = _select_cross_functional_hotspots(events)
    misalignments = detect_misalignments(events, profiles)

    return SharedContextView(
        globally_critical=globally_critical,
        cross_functional_hotspots=cross_functional_hotspots,
        misalignments=misalignments,
        generated_for_user_id=generated_for_user_id,
    )


def _select_globally_critical(
    events: list[CandidateEvent],
) -> list[SharedContextItem]:
    """
    Select events that are globally critical for the org.

    Inclusion criteria (OR logic):
      A. importance > 0.65 AND (urgency > 0.5 OR unresolved > 0.6)
      B. cross_functional > 0.5 AND importance > 0.5

    Sorted by shared_context_score descending, capped at top 5.
    """
    candidates: list[tuple[float, SharedContextItem]] = []

    for event in events:
        sig = _safe_signals(event)

        qualifies_a = (
            sig["importance_score"] > _GLOBALLY_CRITICAL_IMPORTANCE_THRESHOLD
            and (
                sig["urgency_score"] > _GLOBALLY_CRITICAL_URGENCY_THRESHOLD
                or sig["unresolved_score"] > _GLOBALLY_CRITICAL_UNRESOLVED_THRESHOLD
            )
        )
        qualifies_b = (
            sig["cross_functional_score"] > _GLOBALLY_CRITICAL_CROSS_FUNCTIONAL_THRESHOLD
            and sig["importance_score"] > _GLOBALLY_CRITICAL_CROSS_FUNCTIONAL_IMPORTANCE_THRESHOLD
        )

        if not (qualifies_a or qualifies_b):
            continue

        sc_score = _compute_shared_context_score(sig)
        etype = _event_type(event)
        reason = _compose_reason(sig, etype)

        item = SharedContextItem(
            event_id=event.event_id,
            title=_event_title(event),
            reason=reason,
            signal_level=_signal_level(sc_score),
            event_type=etype,
            cross_functional_score=round(sig["cross_functional_score"], 4),
            affected_user_ids=list(event.participant_ids),
            shared_context_score=sc_score,
        )
        candidates.append((sc_score, item))

    candidates.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in candidates[:_MAX_GLOBALLY_CRITICAL]]


def _select_cross_functional_hotspots(
    events: list[CandidateEvent],
) -> list[SharedContextItem]:
    """
    Select events with high cross-functional engagement.

    Criteria:
      - cross_functional_score > 0.6
      - participant_count >= 3

    Sorted by cross_functional_score descending, capped at top 3.
    """
    candidates: list[tuple[float, SharedContextItem]] = []

    for event in events:
        sig = _safe_signals(event)
        cf_score = sig["cross_functional_score"]

        if cf_score <= _HOTSPOT_CROSS_FUNCTIONAL_THRESHOLD:
            continue
        if event.unique_participant_count < _HOTSPOT_MIN_PARTICIPANTS:
            continue

        sc_score = _compute_shared_context_score(sig)
        etype = _event_type(event)

        item = SharedContextItem(
            event_id=event.event_id,
            title=_event_title(event),
            reason=f"Cross-functional activity likely spanning multiple teams (score: {cf_score:.2f}).",
            signal_level=_signal_level(cf_score),
            event_type=etype,
            cross_functional_score=round(cf_score, 4),
            affected_user_ids=list(event.participant_ids),
            shared_context_score=sc_score,
        )
        candidates.append((cf_score, item))

    candidates.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in candidates[:_MAX_CROSS_FUNCTIONAL_HOTSPOTS]]


def detect_misalignments(
    events: list[CandidateEvent],
    profiles: dict[str, UserContextProfile],
) -> list[MisalignmentSignal]:
    """
    Detect probable misalignment patterns across participants of each event.

    A misalignment is flagged when:
      1. High importance (> 0.6) AND many participants (>= 3) with no dominant contributor
         — suggests ambiguous ownership
      2. Cross-functional participants whose primary topic affinity doesn't match
         the event's primary topic label — suggests different camps
      3. A participant's event_type_affinities suggest "status_update" but the event
         is classified as "blocker" — suggests asymmetric urgency perception

    Confidence = min(0.9, cross_functional_score * 1.2) when misalignment detected.
    """
    signals: list[MisalignmentSignal] = []

    for event in events:
        sig = _safe_signals(event)
        importance = sig["importance_score"]
        cf_score = sig["cross_functional_score"]

        # Not interesting enough to analyse
        if importance <= _MISALIGNMENT_IMPORTANCE_THRESHOLD and cf_score <= _MISALIGNMENT_CROSS_FUNCTIONAL_THRESHOLD:
            continue

        etype = _event_type(event)
        event_topics = _topic_labels(event)
        primary_event_topic = event_topics[0] if event_topics else None

        different_camp_ids: list[str] = []
        differing_views: dict[str, str] = {}
        misalignment_reasons: list[str] = []

        for uid in event.participant_ids:
            profile = profiles.get(uid)
            if profile is None:
                continue

            # Pattern 1 + 2: topic camp divergence
            # A participant is in a "different camp" if their primary topic affinity
            # does not align with the event's primary topic label.
            if primary_event_topic and profile.topic_affinities:
                user_primary_topic = max(
                    profile.topic_affinities, key=profile.topic_affinities.get
                )
                if user_primary_topic != primary_event_topic:
                    different_camp_ids.append(uid)

            # Pattern 3: event type perception divergence
            # If the event is a blocker but this participant primarily engages
            # with status_update type events, flag the divergence.
            if etype == "blocker" and profile.event_type_affinities:
                user_top_etype = max(
                    profile.event_type_affinities,
                    key=profile.event_type_affinities.get,
                )
                if user_top_etype == "status_update":
                    differing_views[uid] = "status_update"

        # Evaluate pattern 1: high importance + many participants = ownership ambiguity
        if (
            importance > _MISALIGNMENT_IMPORTANCE_THRESHOLD
            and event.unique_participant_count >= _MISALIGNMENT_MIN_PARTICIPANTS
        ):
            misalignment_reasons.append(
                f"High-importance event ({importance:.2f}) with {event.unique_participant_count} "
                f"participants and no clear dominant contributor — ownership may be ambiguous"
            )

        # Evaluate pattern 2: camp divergence across cross-functional participants
        if cf_score > _MISALIGNMENT_CROSS_FUNCTIONAL_THRESHOLD and len(different_camp_ids) >= 2:
            misalignment_reasons.append(
                f"Participants from different topic clusters involved — "
                f"{len(different_camp_ids)} participant(s) appear to be from a different domain than the event's primary topic"
            )

        # Evaluate pattern 3: urgency perception gap
        if differing_views:
            misalignment_reasons.append(
                f"Event is classified as blocker but {len(differing_views)} participant(s) "
                f"primarily engage with status_update events — urgency perception may differ"
            )

        if not misalignment_reasons:
            continue

        confidence = round(min(0.9, cf_score * 1.2), 4)

        signal = MisalignmentSignal(
            event_id=event.event_id,
            misalignment_flag=True,
            misalignment_reason="; ".join(misalignment_reasons),
            affected_function_ids=different_camp_ids,
            differing_event_type_views=differing_views,
            confidence=confidence,
        )
        signals.append(signal)

    return signals
