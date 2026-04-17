"""
Ownership and accountability inference for CandidateEvents.

All inferences derive exclusively from Slack data (messages, threads, mentions).
No Jira, org charts, or external systems are assumed.

All outputs are probabilistic — use hedged language and confidence scores accordingly.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from src.models import CandidateEvent, SlackWorkspace
from .ownership_models import OwnershipSignals

# Signal weights — must sum to 1.0
_W_ROOT = 0.30
_W_REPLY_DOMINANCE = 0.35
_W_MENTION = 0.20
_W_ACTION = 0.15

# Action-taking phrases that suggest a user is taking ownership
_ACTION_PHRASES = [
    "i'll fix",
    "i will",
    "i'm working on",
    "i am working on",
    "i updated",
    "i sent",
    "i confirmed",
]

# Map topic labels to human-readable function / team names
_TOPIC_TO_FUNCTION: dict[str, str] = {
    "firmware": "Firmware",
    "supply_chain": "Supply Chain",
    "thermal": "Hardware",
    "pcb": "Hardware",
    "bms": "Hardware",
    "connector": "Supply Chain",
    "testing": "Test/QA",
    "sensor": "Hardware",
    "nor_flash": "Firmware",
}

# Scheduling/PM topics map differently
_SCHEDULING_LABELS = {"scheduling", "planning", "roadmap", "milestone"}


def infer_ownership(
    event: CandidateEvent,
    workspace: SlackWorkspace,
) -> OwnershipSignals:
    """
    Infer likely owner and accountability signals for a CandidateEvent.

    Uses only Slack-native signals:
    - root authorship
    - reply dominance
    - mention patterns
    - action-taking language

    Returns an OwnershipSignals object. All scores are probabilistic.
    """
    thread_messages = [m for m in workspace.messages if m.thread_id == event.thread_id]

    participants = list(event.participant_ids)
    if not participants:
        return _accountability_gap(
            "no participants found in event",
            event,
        )

    # --- Score each candidate ---
    scores: dict[str, float] = {uid: 0.0 for uid in participants}
    evidence: list[str] = []

    # 1. Root authorship
    root_author = _find_root_author(event, thread_messages)
    if root_author and root_author in scores:
        root_also_replied = any(
            m.user_id == root_author and not m.is_thread_root
            for m in thread_messages
        )
        if root_also_replied:
            scores[root_author] += _W_ROOT
            evidence.append(
                f"{root_author} likely started this thread and followed up with replies"
            )
        else:
            # Root only — partial credit
            scores[root_author] += _W_ROOT * 0.5
            evidence.append(f"{root_author} appears to have opened this thread")

    # 2. Reply dominance
    reply_messages = [m for m in thread_messages if not m.is_thread_root]
    total_replies = len(reply_messages)
    if total_replies > 0:
        reply_counts = Counter(m.user_id for m in reply_messages)
        for uid, count in reply_counts.items():
            if uid in scores:
                dominance = count / total_replies
                scores[uid] += _W_REPLY_DOMINANCE * dominance
        top_replier, top_count = reply_counts.most_common(1)[0]
        if top_replier in scores and total_replies > 0:
            dominance_pct = round(top_count / total_replies * 100)
            evidence.append(
                f"{top_replier} appears most active in replies "
                f"({top_count}/{total_replies} messages, ~{dominance_pct}% of replies)"
            )

    # 3. Mention patterns
    mention_counts: Counter[str] = Counter()
    for m in thread_messages:
        for mentioned_uid in m.mentions:
            if mentioned_uid in scores:
                mention_counts[mentioned_uid] += 1

    total_mentions = sum(mention_counts.values())
    if total_mentions > 0:
        for uid, count in mention_counts.items():
            mention_share = count / total_mentions
            scores[uid] += _W_MENTION * mention_share
        top_mentioned, top_m_count = mention_counts.most_common(1)[0]
        evidence.append(
            f"{top_mentioned} is mentioned {top_m_count} time(s) across this thread, "
            "suggesting domain expertise or accountability"
        )

    # 4. Action-taking language
    action_scores: dict[str, float] = defaultdict(float)
    for m in thread_messages:
        lower_text = m.text.lower()
        hit_count = sum(1 for phrase in _ACTION_PHRASES if phrase in lower_text)
        if hit_count > 0 and m.user_id in scores:
            # Cap contribution per message at 1.0; accumulate across messages
            action_scores[m.user_id] += min(hit_count, 1.0)

    if action_scores:
        max_action = max(action_scores.values())
        for uid, raw in action_scores.items():
            normalised = raw / max(max_action, 1.0)
            scores[uid] += _W_ACTION * normalised
        top_actor = max(action_scores, key=action_scores.__getitem__)
        evidence.append(
            f"{top_actor} used action-taking language suggesting active ownership"
        )

    # --- Normalise scores to [0, 1] ---
    total_score = sum(scores.values())
    if total_score > 0:
        scores = {uid: s / total_score for uid, s in scores.items()}

    # --- Determine likely owner ---
    if not scores:
        return _accountability_gap("no scoreable participants", event)

    best_uid = max(scores, key=scores.__getitem__)
    best_confidence = round(scores[best_uid], 3)

    likely_owner: str | None = None
    if best_confidence >= 0.3:
        likely_owner = best_uid

    # Key contributors: all other participants above threshold
    key_contributors = [
        uid for uid, s in sorted(scores.items(), key=lambda x: -x[1])
        if uid != likely_owner and s >= 0.1
    ]

    # --- Accountability gap detection ---
    gap_flag = False
    gap_reason = ""

    if best_confidence < 0.3:
        gap_flag = True
        gap_reason = "no dominant contributor — ownership is unclear"
    elif _is_blocker_or_risk(event) and best_confidence < 0.5:
        gap_flag = True
        gap_reason = (
            "this appears to be a blocker or risk event but owner confidence is low "
            f"({best_confidence:.2f})"
        )
    elif len(participants) == 1 and total_replies == 0:
        gap_flag = True
        gap_reason = "single-participant thread with no replies — may be dropped or ignored"

    # Single participant + no replies always flags gap, regardless of above
    if len(participants) == 1 and total_replies == 0 and not gap_flag:
        gap_flag = True
        gap_reason = "single-participant thread with no replies — may be dropped or ignored"

    # --- Function / team inference from topic labels ---
    function_or_team = _infer_function(event)

    return OwnershipSignals(
        likely_owner_user_id=likely_owner,
        likely_owner_confidence=best_confidence,
        key_contributor_ids=key_contributors,
        likely_function_or_team=function_or_team,
        accountability_gap_flag=gap_flag,
        accountability_gap_reason=gap_reason,
        ownership_evidence=evidence,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_root_author(event: CandidateEvent, thread_messages: list) -> str | None:
    """Return the user_id of the thread root message, if determinable."""
    root_msgs = [m for m in thread_messages if m.is_thread_root]
    if root_msgs:
        return root_msgs[0].user_id
    # Fallback: earliest message in the thread
    if thread_messages:
        return min(thread_messages, key=lambda m: m.timestamp).user_id
    # Last fallback: first participant listed on the event
    if event.participant_ids:
        return event.participant_ids[0]
    return None


def _is_blocker_or_risk(event: CandidateEvent) -> bool:
    """Return True if the event appears to be a blocker or risk type."""
    if event.signals is None:
        return False
    return event.signals.dominant_event_type in ("blocker", "risk")


def _infer_function(event: CandidateEvent) -> str | None:
    """
    Infer the responsible function/team from topic_labels on the event signals.
    Returns the most specific match, or None if no mapping exists.
    """
    if event.signals is None:
        return None

    topic_labels = event.signals.topic_labels
    if not topic_labels:
        return None

    # Check for scheduling/PM indicators directly on topic text
    combined = " ".join(topic_labels).lower()
    if any(lbl in combined for lbl in _SCHEDULING_LABELS):
        return "Program Management"

    # Map first matching label
    for label in topic_labels:
        mapped = _TOPIC_TO_FUNCTION.get(label)
        if mapped:
            return mapped

    return None


def _accountability_gap(reason: str, event: CandidateEvent) -> OwnershipSignals:
    """Return an OwnershipSignals indicating a clear accountability gap."""
    return OwnershipSignals(
        likely_owner_user_id=None,
        likely_owner_confidence=0.0,
        key_contributor_ids=[],
        likely_function_or_team=_infer_function(event),
        accountability_gap_flag=True,
        accountability_gap_reason=reason,
        ownership_evidence=[],
    )
