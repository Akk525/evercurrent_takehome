"""
Summarization stage.

Runs only on the top-k selected items (post-ranking).
LLM is never called on candidates that didn't make the cut.
"""

from __future__ import annotations

from src.models import CandidateEvent, RankedDigestItem, UserContextProfile
from .providers import LLMProvider, FallbackProvider


def summarize_digest_items(
    items: list[RankedDigestItem],
    events_by_id: dict[str, CandidateEvent],
    profile: UserContextProfile,
    provider: LLMProvider | None = None,
) -> list[RankedDigestItem]:
    """
    Fill in `summary` and `why_shown` for each ranked item.

    If provider is None, uses FallbackProvider (no LLM calls).
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

        summary, why_shown = provider.summarize(event, item, profile)
        item.summary = summary
        item.why_shown = why_shown

    return items
