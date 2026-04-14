"""
Digest assembly: the final stage that wires all pipeline components together.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.models import DailyDigest, RankedDigestItem, UserContextProfile, SharedEventSummary
from src.models import SlackWorkspace, CandidateEvent
from src.ingest import load_workspace
from src.events import build_candidate_events
from src.enrichment import enrich_candidate_events
from src.profiles import build_user_profiles
from src.ranking import rank_events_for_user, RankingConfig
from src.embeddings import EmbeddingStore
from src.summarization import summarize_digest_items, build_shared_summaries, FallbackProvider
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
    config: RankingConfig | None = None,
    embedding_store: EmbeddingStore | None = None,
    include_excluded: bool = False,
    shared_summaries: dict[str, SharedEventSummary] | None = None,
) -> DailyDigest:
    """
    Assemble a DailyDigest for a single user from enriched events.

    If shared_summaries is provided, event-level summaries are reused from it
    and only why_shown is generated per-user.
    """
    if now is None:
        now = datetime.now(tz=timezone.utc)
    if date_str is None:
        date_str = now.strftime("%Y-%m-%d")

    # Rank — returns (selected, excluded)
    ranked_items, excluded_items = rank_events_for_user(
        enriched_events,
        profile,
        top_k=top_k,
        weights=weights,
        now=now,
        config=config,
        embedding_store=embedding_store,
        include_excluded=include_excluded,
    )

    # Summarize (LLM or fallback), reusing shared summaries where available
    ranked_items = summarize_digest_items(
        ranked_items,
        events_by_id=events_by_id,
        profile=profile,
        provider=provider,
        shared_summaries=shared_summaries,
    )

    headline = _generate_headline(ranked_items)

    return DailyDigest(
        user_id=user_id,
        date=date_str,
        headline=headline,
        items=ranked_items,
        generated_at=now,
        total_candidates_considered=len(enriched_events),
        llm_used=provider is not None and not isinstance(provider, FallbackProvider),
        excluded_items=excluded_items,
    )


def _generate_headline(items: list[RankedDigestItem]) -> str:
    """
    Build a plain-English digest headline.

    Avoids double-counting: blockers/risks that are already counted as
    high-signal are not listed again as a separate count. Instead we lead
    with the most critical type present, then note the total.
    """
    if not items:
        return "No significant updates today."

    n = len(items)
    blockers = [i for i in items if i.event_type == "blocker"]
    risks = [i for i in items if i.event_type == "risk"]
    decisions = [i for i in items if i.event_type == "decision"]
    high_urgency = [i for i in items if i.signal_level == "high"]

    parts = []

    if blockers:
        nb = len(blockers)
        parts.append(f"{nb} likely blocker{'s' if nb > 1 else ''}")
    if risks:
        nr = len(risks)
        parts.append(f"{nr} risk item{'s' if nr > 1 else ''}")
    if decisions and not parts:
        nd = len(decisions)
        parts.append(f"{nd} pending decision{'s' if nd > 1 else ''}")

    if not parts:
        # Nothing notable by type — fall back to signal level summary
        if high_urgency:
            nh = len(high_urgency)
            return f"{nh} high-signal update{'s' if nh > 1 else ''} ({n} total items)."
        return f"{n} update{'s' if n > 1 else ''} to review."

    headline = " and ".join(parts) + f" — {n} item{'s' if n > 1 else ''} total."
    return headline


def run_full_pipeline(
    data_dir: Path,
    user_ids: list[str] | None = None,
    top_k: int = 5,
    weights: dict[str, float] | None = None,
    provider: LLMProvider | None = None,
    now: datetime | None = None,
    date_str: str | None = None,
    config: RankingConfig | None = None,
    embedding_store: EmbeddingStore | None = None,
    include_excluded: bool = False,
    metrics=None,  # Optional[PipelineMetrics] — avoids circular import
) -> dict[str, DailyDigest]:
    """
    Run the full digest pipeline for all (or specified) users.

    Returns a dict keyed by user_id.
    If metrics is provided (a PipelineMetrics instance), stage timings are recorded.
    """
    from src.observability import StageTimer

    if now is None:
        now = datetime.now(tz=timezone.utc)
    if date_str is None:
        date_str = now.strftime("%Y-%m-%d")

    # 1. Ingest
    workspace = load_workspace(data_dir)

    # 2. Candidate event construction
    events = build_candidate_events(workspace)

    # 3. Semantic enrichment (builds embedding store internally if not provided)
    with StageTimer.measure("enrichment") as t:
        enriched = enrich_candidate_events(
            events, workspace, now=now, embedding_store=embedding_store
        )
    if metrics is not None:
        metrics.record_stage(t)
        metrics.total_candidate_events = len(enriched)
        metrics.events_enriched = len(enriched)

    # 4. Expose the embedding store that was built during enrichment
    # so the ranker can use the same store without re-fitting
    if embedding_store is None:
        from src.enrichment.enricher import _build_embedding_store
        embedding_store = _build_embedding_store(enriched)

    # 5. User profiles (with interaction-weighted affinities)
    with StageTimer.measure("profiling") as t:
        profiles = build_user_profiles(workspace, enriched, now=now)
    if metrics is not None:
        metrics.record_stage(t)

    # 6. Build event lookup
    events_by_id = {e.event_id: e for e in enriched}

    # 7. Build shared summaries once for all events — reused across all users
    with StageTimer.measure("shared_summarization") as t:
        all_event_ids = [e.event_id for e in enriched]
        shared = build_shared_summaries(events_by_id, all_event_ids, provider=provider)
    if metrics is not None:
        metrics.record_stage(t)
        metrics.summaries_generated = len(shared)

    # 8. Target user set
    target_users = user_ids if user_ids else [u.user_id for u in workspace.users]

    # 9. Per-user digest
    digests: dict[str, DailyDigest] = {}
    with StageTimer.measure("ranking_and_digest") as t:
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
                config=config,
                embedding_store=embedding_store,
                include_excluded=include_excluded,
                shared_summaries=shared,
            )
            digests[uid] = digest
            if metrics is not None:
                metrics.users_processed += 1
                # Count events actually scored for this user
                metrics.total_candidates_scored += len(enriched)
                # Items served from shared summary pool (pre-generated, reused)
                metrics.summaries_reused += len(digest.items)
    if metrics is not None:
        metrics.record_stage(t)
        metrics.pipeline_mode = "full"

    return digests


def run_offline_enrichment(
    data_dir: Path,
    output_path: Path,
    now: datetime | None = None,
) -> dict:
    """
    Offline enrichment pass: ingest → events → enrich → profiles.

    Serialises enriched events and profiles to a JSON file at output_path
    for later use by run_online_digest().

    Returns the serialised dict.
    """
    if now is None:
        now = datetime.now(tz=timezone.utc)

    workspace = load_workspace(data_dir)
    events = build_candidate_events(workspace)
    enriched = enrich_candidate_events(events, workspace, now=now)
    profiles = build_user_profiles(workspace, enriched, now=now)

    payload = {
        "enriched_events": [e.model_dump(mode="json") for e in enriched],
        "profiles": {uid: p.model_dump(mode="json") for uid, p in profiles.items()},
        "workspace": workspace.model_dump(mode="json"),
        "enriched_at": now.isoformat(),
    }

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2))
    print(f"[offline] Wrote enrichment snapshot: {output_path}")

    return payload


def run_online_digest(
    enrichment_path: Path,
    user_ids: list[str] | None = None,
    top_k: int = 5,
    provider: LLMProvider | None = None,
    now: datetime | None = None,
    date_str: str | None = None,
    config: RankingConfig | None = None,
    include_excluded: bool = False,
) -> dict[str, DailyDigest]:
    """
    Online digest pass: loads a pre-computed enrichment snapshot and runs
    ranking + summarization only.

    Faster than run_full_pipeline() because enrichment is pre-computed.
    """
    if now is None:
        now = datetime.now(tz=timezone.utc)
    if date_str is None:
        date_str = now.strftime("%Y-%m-%d")

    enrichment_path = Path(enrichment_path)
    payload = json.loads(enrichment_path.read_text())

    # Deserialise
    enriched = [CandidateEvent.model_validate(e) for e in payload["enriched_events"]]
    profiles_raw = payload["profiles"]
    profiles = {uid: UserContextProfile.model_validate(p) for uid, p in profiles_raw.items()}

    # Rebuild workspace for user list (lightweight)
    from src.models.raw import SlackWorkspace as RawWorkspace
    workspace = RawWorkspace.model_validate(payload["workspace"])

    # Rebuild embedding store from enriched events (must match enrichment-time corpus)
    from src.enrichment.enricher import _build_embedding_store
    embedding_store = _build_embedding_store(enriched)

    events_by_id = {e.event_id: e for e in enriched}

    # Build shared summaries once
    all_event_ids = [e.event_id for e in enriched]
    shared = build_shared_summaries(events_by_id, all_event_ids, provider=provider)

    target_users = user_ids if user_ids else [u.user_id for u in workspace.users]

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
            provider=provider,
            now=now,
            date_str=date_str,
            config=config,
            embedding_store=embedding_store,
            include_excluded=include_excluded,
            shared_summaries=shared,
        )
        digests[uid] = digest

    return digests
