"""
Summarization stage.

Runs only on the top-k selected items (post-ranking).
LLM is never called on candidates that didn't make the cut.
"""

from __future__ import annotations

from src.models import CandidateEvent, RankedDigestItem, UserContextProfile, SharedEventSummary
from .providers import LLMProvider, FallbackProvider


def summarize_digest_items(
    items: list[RankedDigestItem],
    events_by_id: dict[str, CandidateEvent],
    profile: UserContextProfile,
    provider: LLMProvider | None = None,
    shared_summaries: dict[str, SharedEventSummary] | None = None,
) -> list[RankedDigestItem]:
    """
    Fill in `summary` and `why_shown` for each ranked item.

    If provider is None, uses FallbackProvider (no LLM calls).
    If shared_summaries is provided, event-level summaries are reused from it
    and only `why_shown` is generated per-user — avoiding redundant summarization.
    Returns the same list with fields populated in-place.
    """
    if provider is None:
        provider = FallbackProvider()

    for item in items:
        event = events_by_id.get(item.event_id)
        if event is None:
            item.summary = "Source event not found."
            item.why_shown = "Unknown."
            continue

        if shared_summaries is not None and item.event_id in shared_summaries:
            # Reuse pre-computed shared summary; only generate why_shown per-user
            item.summary = shared_summaries[item.event_id].summary
            _, why_shown = provider.summarize(event, item, profile)
            item.why_shown = why_shown
        else:
            summary, why_shown = provider.summarize(event, item, profile)
            item.summary = summary
            item.why_shown = why_shown

    return items


def build_shared_summaries(
    events_by_id: dict[str, CandidateEvent],
    event_ids: list[str],
    provider: LLMProvider | None = None,
) -> dict[str, SharedEventSummary]:
    """
    Build shared event summaries for all requested event_ids.
    Each unique event is summarized exactly once, regardless of how many users see it.

    Returns dict[event_id, SharedEventSummary].
    """
    if provider is None:
        provider = FallbackProvider()

    result: dict[str, SharedEventSummary] = {}
    seen: set[str] = set()

    for event_id in event_ids:
        if event_id in seen:
            continue
        seen.add(event_id)

        event = events_by_id.get(event_id)
        if event is None:
            continue

        summary_text = provider.summarize_shared(event)

        signals = event.signals
        result[event_id] = SharedEventSummary(
            event_id=event_id,
            title=signals.title if signals else event_id,
            summary=summary_text,
            event_type=signals.dominant_event_type if signals else "noise",
            unresolved=(signals.unresolved_score > 0.5) if signals else False,
            confidence=signals.confidence if signals else 0.0,
        )

    return result
