"""
Digest assembly: the final stage that wires all pipeline components together.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.models import DailyDigest, RankedDigestItem, UserContextProfile
from src.models import SlackWorkspace, CandidateEvent
from src.ingest import load_workspace
from src.events import build_candidate_events
from src.enrichment import enrich_candidate_events
from src.profiles import build_user_profiles
from src.ranking import rank_events_for_user, DEFAULT_WEIGHTS
from src.summarization import summarize_digest_items, FallbackProvider
from src.summarization.providers import LLMProvider


def assemble_digest(
    user_id: str,
    enriched_events: list[CandidateEvent],
    profile: UserContextProfile,
    events_by_id: dict[str, CandidateEvent],
    top_k: int = 5,
    weights: dict[str, float] | None = None,
    provider: LLMProvider | None = None,
    now: datetime | None = None,
    date_str: str | None = None,
) -> DailyDigest:
    """
    Assemble a DailyDigest for a single user from enriched events.
    """
    if now is None:
        now = datetime.now(tz=timezone.utc)
    if date_str is None:
        date_str = now.strftime("%Y-%m-%d")

    # Rank
    ranked_items = rank_events_for_user(
        enriched_events,
        profile,
        top_k=top_k,
        weights=weights,
        now=now,
    )

    # Summarize (LLM or fallback)
    ranked_items = summarize_digest_items(
        ranked_items,
        events_by_id=events_by_id,
        profile=profile,
        provider=provider,
    )

    # Headline
    headline = _generate_headline(ranked_items)

    return DailyDigest(
        user_id=user_id,
        date=date_str,
        headline=headline,
        items=ranked_items,
        generated_at=now,
        total_candidates_considered=len(enriched_events),
        llm_used=provider is not None and not isinstance(provider, FallbackProvider),
    )


def _generate_headline(items: list[RankedDigestItem]) -> str:
    """Build a plain-English headline summarising the digest."""
    if not items:
        return "No significant updates today."

    high_count = sum(1 for i in items if i.signal_level == "high")
    blockers = [i for i in items if i.event_type in ("blocker", "risk") and i.signal_level in ("high", "medium")]

    parts = []
    if high_count > 0:
        parts.append(f"{high_count} high-signal update{'s' if high_count > 1 else ''}")
    if blockers:
        parts.append(f"{len(blockers)} likely blocker{'s' if len(blockers) > 1 else ''} or risk{'s' if len(blockers) > 1 else ''}")

    if not parts:
        return f"{len(items)} update{'s' if len(items) > 1 else ''} to review."

    return " and ".join(parts) + "."


def run_full_pipeline(
    data_dir: Path,
    user_ids: list[str] | None = None,
    top_k: int = 5,
    weights: dict[str, float] | None = None,
    provider: LLMProvider | None = None,
    now: datetime | None = None,
    date_str: str | None = None,
) -> dict[str, DailyDigest]:
    """
    Run the full digest pipeline for all (or specified) users.

    Returns a dict keyed by user_id.
    """
    if now is None:
        now = datetime.now(tz=timezone.utc)
    if date_str is None:
        date_str = now.strftime("%Y-%m-%d")

    # 1. Ingest
    workspace = load_workspace(data_dir)

    # 2. Candidate event construction
    events = build_candidate_events(workspace)

    # 3. Semantic enrichment
    enriched = enrich_candidate_events(events, workspace, now=now)

    # 4. User profiles
    profiles = build_user_profiles(workspace, enriched)

    # 5. Build event lookup
    events_by_id = {e.event_id: e for e in enriched}

    # 6. Target user set
    target_users = user_ids if user_ids else [u.user_id for u in workspace.users]

    # 7. Per-user digest
    digests: dict[str, DailyDigest] = {}
    for uid in target_users:
        if uid not in profiles:
            continue
        digest = assemble_digest(
            user_id=uid,
            enriched_events=enriched,
            profile=profiles[uid],
            events_by_id=events_by_id,
            top_k=top_k,
            weights=weights,
            provider=provider,
            now=now,
            date_str=date_str,
        )
        digests[uid] = digest

    return digests
