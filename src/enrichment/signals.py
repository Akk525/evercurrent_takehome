"""
Heuristic signal computation for a single candidate event.

All functions return values in [0, 1].
The approach is intentionally rule-based and inspectable.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from src.models import CandidateEvent, SlackWorkspace
from .keywords import (
    BLOCKER_KEYWORDS, DECISION_KEYWORDS, RISK_KEYWORDS,
    URGENCY_KEYWORDS, NOISE_KEYWORDS, REQUEST_FOR_INPUT_KEYWORDS,
    STATUS_UPDATE_KEYWORDS, TOPIC_MAP,
)


def _keyword_hit_rate(text: str, keywords: list[str]) -> float:
    """Fraction of keywords matched in the text (capped at 1.0)."""
    lower = text.lower()
    hits = sum(1 for kw in keywords if kw in lower)
    # Scale: more than 3 hits = fully saturated
    return min(hits / 3.0, 1.0)


def compute_event_type_scores(event: CandidateEvent) -> dict[str, float]:
    """
    Independently score each event type from keyword signals.
    Not a probabilistic distribution — treat as independent signals.
    """
    text = event.text_bundle

    return {
        "blocker": _keyword_hit_rate(text, BLOCKER_KEYWORDS),
        "decision": _keyword_hit_rate(text, DECISION_KEYWORDS),
        "status_update": _keyword_hit_rate(text, STATUS_UPDATE_KEYWORDS),
        "risk": _keyword_hit_rate(text, RISK_KEYWORDS),
        "request_for_input": _keyword_hit_rate(text, REQUEST_FOR_INPUT_KEYWORDS),
        "noise": _keyword_hit_rate(text, NOISE_KEYWORDS),
    }


def compute_urgency(event: CandidateEvent) -> float:
    """
    Urgency = combination of:
    - urgency keyword density
    - whether participants used explicit deadline language
    - reaction count (alert, eyes reactions signal urgency)
    """
    text_score = _keyword_hit_rate(event.text_bundle, URGENCY_KEYWORDS)

    # Reaction boost: reactions on messages signal engagement / concern
    reaction_boost = min(event.total_reactions / 15.0, 0.3)

    return min(text_score + reaction_boost, 1.0)


def compute_momentum(event: CandidateEvent) -> float:
    """
    Momentum = thread activity density.
    High reply count + multiple participants in a short window = high momentum.
    """
    # Duration in hours
    duration_hours = max(
        (event.last_activity_at - event.started_at).total_seconds() / 3600.0,
        0.1,
    )
    # Messages per hour as a proxy for engagement velocity
    msg_rate = event.message_count / duration_hours
    # Normalise: 3+ messages/hour = high momentum
    rate_score = min(msg_rate / 3.0, 1.0)

    # Participant diversity boosts momentum
    participant_score = min(event.unique_participant_count / 4.0, 1.0)

    return min(0.6 * rate_score + 0.4 * participant_score, 1.0)


def compute_novelty(
    event: CandidateEvent,
    all_events: list[CandidateEvent],
    now: datetime,
) -> float:
    """
    Novelty = how distinct is this event from other recent events?
    Simple heuristic: if this event's topics overlap heavily with others
    in the same channel on the same day, novelty is lower.

    We use topic label overlap as a proxy.
    """
    event_topics = set(_extract_topic_labels(event.text_bundle))
    if not event_topics:
        return 0.5  # Unknown — default to middle

    # Look at other events in the same channel
    peers = [
        e for e in all_events
        if e.channel_id == event.channel_id and e.event_id != event.event_id
    ]

    if not peers:
        return 0.9  # Only event in channel — high novelty

    overlaps = []
    for peer in peers:
        peer_topics = set(_extract_topic_labels(peer.text_bundle))
        if not peer_topics:
            continue
        overlap = len(event_topics & peer_topics) / len(event_topics | peer_topics)
        overlaps.append(overlap)

    if not overlaps:
        return 0.9

    avg_overlap = sum(overlaps) / len(overlaps)
    return max(0.0, 1.0 - avg_overlap)


def compute_unresolved(event: CandidateEvent) -> float:
    """
    Unresolved signal = does the thread end without clear resolution?

    Heuristics:
    - Last message contains a question mark or open action item
    - Thread has "will update" / "TBD" / "need to" language without a follow-up "done" / "fixed"
    - High urgency score but no resolution markers
    """
    text = event.text_bundle.lower()

    open_signals = [
        "?", "will update", "tbd", "to be determined", "need to", "will check",
        "investigating", "working on", "by eod", "will share", "let me check",
    ]
    closed_signals = [
        "resolved", "fixed", "done", "confirmed", "decision made",
        "closed", "completed", "will not block",
    ]

    open_hits = sum(1 for s in open_signals if s in text)
    closed_hits = sum(1 for s in closed_signals if s in text)

    raw = (open_hits - closed_hits) / max(open_hits + closed_hits, 1)
    return max(0.0, min(raw, 1.0))


def compute_cross_functional(event: CandidateEvent, workspace: SlackWorkspace) -> float:
    """
    Cross-functional score = do participants span multiple channels/topic areas?
    High cross-functional activity often signals broader impact.
    """
    channel_by_id = {c.channel_id: c for c in workspace.channels}
    user_by_id = {u.user_id: u for u in workspace.users}

    # Collect the set of channels that participants are active in
    participant_channels: set[str] = set()
    for uid in event.participant_ids:
        user = user_by_id.get(uid)
        if user:
            participant_channels.update(user.channel_ids)

    # Normalise over total channels
    total_channels = len(workspace.channels)
    if total_channels == 0:
        return 0.0

    return min(len(participant_channels) / total_channels, 1.0)


def compute_importance(
    event: CandidateEvent,
    type_scores: dict[str, float],
) -> float:
    """
    Importance is a composite of:
    - blocker and risk signals (highest weight)
    - decision signals
    - participant count (more participants = broader impact)
    - reaction count
    """
    blocker_w = 0.35
    risk_w = 0.25
    decision_w = 0.15
    participant_w = 0.15
    reaction_w = 0.10

    participant_score = min(event.unique_participant_count / 5.0, 1.0)
    reaction_score = min(event.total_reactions / 10.0, 1.0)

    return (
        blocker_w * type_scores.get("blocker", 0.0)
        + risk_w * type_scores.get("risk", 0.0)
        + decision_w * type_scores.get("decision", 0.0)
        + participant_w * participant_score
        + reaction_w * reaction_score
    )


def compute_recency(event: CandidateEvent, now: datetime) -> float:
    """
    Recency score based on hours since last activity.
    Decays from 1.0 (just now) to ~0.0 (48+ hours ago).
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    last_at = event.last_activity_at
    if last_at.tzinfo is None:
        last_at = last_at.replace(tzinfo=timezone.utc)

    hours_ago = (now - last_at).total_seconds() / 3600.0
    # Exponential decay: half-life ~12 hours
    return max(0.0, 2 ** (-hours_ago / 12.0))


def _extract_topic_labels(text: str) -> list[str]:
    """Match text against the topic keyword map and return matching labels."""
    lower = text.lower()
    labels = []
    for topic, keywords in TOPIC_MAP.items():
        if any(kw in lower for kw in keywords):
            labels.append(topic)
    return labels


def compute_title(event: CandidateEvent, type_scores: dict[str, float]) -> str:
    """
    Generate a short provisional title for the event.
    Uses the first non-trivial sentence of the root message as a base,
    then prepends a type indicator.
    """
    # Get the first message text (root)
    lines = event.text_bundle.split("\n")
    if not lines:
        return "Discussion thread"

    first_line = lines[0]
    # Strip the "[user_id]: " prefix
    if "]: " in first_line:
        first_line = first_line.split("]: ", 1)[1]

    # Truncate to first sentence or 100 chars
    sentence = re.split(r"[.!?]", first_line)[0].strip()
    if len(sentence) > 100:
        sentence = sentence[:97] + "..."

    # Prepend type label
    dominant = max(type_scores, key=type_scores.get)
    if type_scores.get("noise", 0) > 0.5:
        return f"[Social] {sentence}"
    elif dominant in ("blocker", "risk"):
        return f"[{dominant.upper()}] {sentence}"
    elif dominant == "decision":
        return f"[Decision] {sentence}"
    else:
        return sentence
