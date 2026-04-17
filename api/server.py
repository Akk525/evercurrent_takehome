"""
FastAPI server wrapping the digest engine for the Slack-like demo UI.

Serves:
- Workspace data (users, channels)
- Channel message feeds
- Thread details + reply posting
- Per-user digests (pre-computed at startup, refreshed on new activity)
- DM messages (in-memory + SQLite backed)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Body
from fastapi.middleware.cors import CORSMiddleware

from src.ingest import load_workspace
from src.events import build_candidate_events
from src.enrichment import enrich_candidate_events
from src.enrichment.enricher import _build_embedding_store
from src.issue_linking.linker import build_issue_clusters
from src.issue_memory.store import IssueMemoryStore
from src.issue_memory.matcher import match_and_update_issues
from src.enrichment.ownership import infer_ownership
from src.enrichment.drift import detect_drift
from src.impact.graph import build_issue_graph
from src.impact.graph_models import IssueGraph
from src.profiles import build_user_profiles
from src.digest.assembler import assemble_digest
from src.summarization import build_shared_summaries
from src.models.raw import SlackWorkspace, SlackMessage as RawSlackMessage

from api.persistence import (
    init_db,
    load_dm_messages,
    save_dm_message,
    load_thread_replies,
    save_thread_reply,
)
from src.slack_ingest.reconciler import ReconciliationWorker
from src.slack_ingest.socket_mode import SocketModeManager
from src.slack_ingest.adapter import load_workspace_from_slack_store
from src.models.trace import StageTrace, EventPipelineTrace

DATA_DIR = Path(__file__).parent.parent / "data" / "mock_slack"
NOW = datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc)

logger = logging.getLogger(__name__)

app = FastAPI(title="Digest Engine API")

# Mount Slack Events API router (gracefully no-ops if SLACK_SIGNING_SECRET is absent)
try:
    from api.slack_events import router as slack_router
    app.include_router(slack_router)
except Exception:
    pass  # Slack integration optional — never block startup

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Startup state
_workspace: Optional[SlackWorkspace] = None
_digests: dict = {}
_profiles: dict = {}

# Cached pipeline outputs — computed once per refresh cycle
_enriched_events: list = []
_embedding_store = None
_cached_graph: Optional[IssueGraph] = None
_issue_memory_store: Optional[IssueMemoryStore] = None
_event_traces: dict[str, EventPipelineTrace] = {}

# In-memory DM store: canonical key = "uid_a:uid_b" (sorted)
_dm_messages: dict[str, list[dict]] = defaultdict(list)

# Digest refresh flag: set True when thread replies arrive
_needs_refresh: bool = False

# Slack background integration workers
_reconciler: Optional[ReconciliationWorker] = None
_socket_mode_manager: Optional[SocketModeManager] = None


def _dm_key(a: str, b: str) -> str:
    return ":".join(sorted([a, b]))


def _load_workspace() -> SlackWorkspace:
    """
    Return the best available workspace representation.

    If Slack integration is active and the local store has data, convert it to a
    SlackWorkspace so the engine operates on real Slack data.  Otherwise fall back
    to the mock JSON fixture — local/demo mode always works without Slack credentials.
    """
    bot_token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if bot_token:
        try:
            from api.slack_events import get_store
            slack_store = get_store()
            if slack_store.has_data():
                logger.info("[server] Loading workspace from Slack ingest store")
                return load_workspace_from_slack_store(slack_store)
        except Exception as e:
            logger.warning("[server] Could not load from Slack store (%s) — falling back to mock", e)
    return load_workspace(DATA_DIR)


def _trigger_refresh() -> None:
    """Signal that a refresh is needed due to Slack store changes."""
    global _needs_refresh
    _needs_refresh = True
    logger.info("[server] Refresh triggered by Slack reconciliation")


def _build_trace(event, now: datetime) -> EventPipelineTrace:
    """Assemble a full pipeline trace from a fully-enriched CandidateEvent."""
    stages: list[StageTrace] = []

    # Stage 1: Candidate construction
    stages.append(StageTrace(
        name="candidate",
        label="Candidate Construction",
        status="active",
        outputs={
            "message_count": event.message_count,
            "reply_count": event.reply_count,
            "participants": event.unique_participant_count,
            "started_at": event.started_at.isoformat(),
            "last_activity_at": event.last_activity_at.isoformat(),
        },
    ))

    # Stage 2: Semantic enrichment
    sig = event.signals
    if sig:
        stages.append(StageTrace(
            name="enrichment",
            label="Semantic Enrichment",
            status="active",
            outputs={
                "title": sig.title,
                "dominant_type": sig.dominant_event_type,
                "urgency": round(sig.urgency_score, 3),
                "importance": round(sig.importance_score, 3),
                "momentum": round(sig.momentum_score, 3),
                "novelty": round(sig.novelty_score, 3),
                "unresolved": round(sig.unresolved_score, 3),
                "cross_functional": round(sig.cross_functional_score, 3),
                "topic_labels": sig.topic_labels,
                "confidence": round(sig.confidence, 3),
                "state_change_hint": sig.state_change_hint,
            },
        ))
    else:
        stages.append(StageTrace(name="enrichment", label="Semantic Enrichment", status="skipped"))

    # Stage 3: Issue linking
    if event.issue_cluster_id:
        stages.append(StageTrace(
            name="issue_linking",
            label="Issue Linking",
            status="active",
            outputs={
                "cluster_id": event.issue_cluster_id,
                "related_events": len(event.related_event_ids),
                "issue_status": event.issue_status,
            },
        ))
    else:
        stages.append(StageTrace(
            name="issue_linking",
            label="Issue Linking",
            status="empty",
            outputs={"issue_status": "new — no matching cluster found"},
        ))

    # Stage 4: Issue memory
    mem = getattr(event, "issue_memory_signals", None)
    if mem is not None:
        stages.append(StageTrace(
            name="issue_memory",
            label="Issue Memory",
            status="active",
            outputs={
                "memory_label": mem.memory_label,
                "age_label": mem.age_label,
                "resurfaced_count": mem.resurfaced_count,
                "hours_open": round(mem.issue_age_hours, 1),
                "escalation_count": mem.escalation_count,
                "persistence_score": round(mem.issue_persistence_score, 3),
                "escalation_score": round(mem.issue_escalation_score, 3),
                "is_new": mem.is_new_issue,
                "is_resurfacing": mem.is_resurfacing_issue,
            },
            score_delta=round(0.10 * mem.issue_persistence_score + 0.05 * mem.issue_escalation_score, 4),
        ))
    else:
        stages.append(StageTrace(
            name="issue_memory",
            label="Issue Memory",
            status="empty",
            outputs={"note": "First time seeing this issue — no history yet"},
        ))

    # Stage 5: Ownership
    own = getattr(event, "ownership_signals", None)
    if own is not None:
        stages.append(StageTrace(
            name="ownership",
            label="Ownership Inference",
            status="active",
            outputs={
                "likely_owner": own.likely_owner_user_id or "unclear",
                "confidence": round(own.likely_owner_confidence, 3),
                "accountability_gap": own.accountability_gap_flag,
                "gap_reason": own.accountability_gap_reason or None,
            },
        ))
    else:
        stages.append(StageTrace(name="ownership", label="Ownership Inference", status="empty"))

    # Stage 6: Drift — always show computed values; status reflects whether drift fired
    drift = getattr(event, "drift_signals", None)
    if drift is not None:
        mem_for_drift = getattr(event, "issue_memory_signals", None)
        stages.append(StageTrace(
            name="drift",
            label="Drift / Process Debt",
            status="active" if drift.drift_flag else "empty",
            outputs={
                "drift_flag": drift.drift_flag,
                "process_debt_score": round(drift.process_debt_score, 3),
                "resurfacing_count": drift.resurfacing_count,
                "long_open_flag": drift.long_open_flag,
                "stale_mitigation_flag": drift.stale_mitigation_flag,
                "repeated_without_resolution": drift.repeated_without_resolution,
                "why_no_drift": (
                    None if drift.drift_flag else
                    "resurfaced_count < 2 and hours_open < 48 — no history yet (run pipeline again after issue memory accumulates)"
                    if (mem_for_drift and mem_for_drift.resurfaced_count < 2)
                    else None
                ),
            },
        ))
    else:
        stages.append(StageTrace(
            name="drift",
            label="Drift / Process Debt",
            status="skipped",
            outputs={"note": "No issue memory signals — drift detection requires prior issue history"},
        ))

    # Stage 7: Graph impact
    graph = getattr(event, "graph_signals", None)
    if graph is not None and graph.graph_impact_boost > 0:
        stages.append(StageTrace(
            name="graph",
            label="Dependency Graph Impact",
            status="active",
            outputs={
                "downstream_blocked": graph.downstream_impact_count,
                "upstream_dependencies": graph.upstream_dependency_count,
                "centrality_score": round(graph.graph_centrality_score, 3),
                "graph_impact_boost": round(graph.graph_impact_boost, 4),
            },
            score_delta=round(graph.graph_impact_boost * 0.05, 4),
        ))
    else:
        stages.append(StageTrace(
            name="graph",
            label="Dependency Graph Impact",
            status="empty",
            outputs={"note": "No downstream dependencies detected"},
        ))

    # Stage 8: Ranking
    # Derive top driver from weighted feature contributions
    features_map = {}
    if sig:
        w = {"user_affinity": 0.28, "importance": 0.24, "urgency": 0.20,
             "momentum": 0.10, "novelty": 0.08, "recency": 0.05, "embedding_affinity": 0.05}
        features_map = {
            "user_affinity": 0.0,  # can't compute without profile here — omit
            "importance": sig.importance_score * w["importance"],
            "urgency": sig.urgency_score * w["urgency"],
            "momentum": sig.momentum_score * w["momentum"],
            "novelty": sig.novelty_score * w["novelty"],
        }
        top_driver = max(features_map, key=features_map.get)
        top_driver_value = round(features_map[top_driver], 3)
    else:
        top_driver = "unknown"
        top_driver_value = 0.0

    return EventPipelineTrace(
        event_id=event.event_id,
        thread_id=event.thread_id,
        channel_id=event.channel_id,
        text_preview=event.text_bundle[:200],
        stages=stages,
        final_score=0.0,   # updated after ranking runs
        top_driver=top_driver,
        top_driver_value=top_driver_value,
        generated_at=now.isoformat(),
    )


def _run_pipeline(workspace: SlackWorkspace, now: datetime) -> dict:
    """
    Run all pipeline stages in the correct dependency order.

    Stage order (each stage depends on the previous):
      1. Candidate event construction
      2. Semantic enrichment (signals, entities, hybrid classification)
      3. Issue linking / clustering (needs entities + embeddings)
      4. Issue memory matching (needs clusters + entity fingerprints)
      5. Ownership inference (after memory — richer issue context)
      6. Drift detection (after memory — reads issue_memory_signals)
      7. Dependency & impact graph construction (after clustering + enrichment)
         → graph_signals attached to events BEFORE ranking
      8. User profile building

    Returns dict with keys:
        enriched, embedding_store, clusters, graph, graph_signals
    """
    global _issue_memory_store

    # Stage 1: candidate events
    events = build_candidate_events(workspace)

    # Stage 2: semantic enrichment — build embedding store once, pass it in
    embedding_store = _build_embedding_store(events)
    enriched = enrich_candidate_events(events, workspace, now=now, embedding_store=embedding_store)

    # Stage 3: issue linking
    clusters = build_issue_clusters(enriched, embedding_store)

    # Stage 4: issue memory matching
    if _issue_memory_store is None:
        _issue_memory_store = IssueMemoryStore()
    match_and_update_issues(enriched, _issue_memory_store, now)

    # Stage 5: ownership inference (has issue memory context now)
    for event in enriched:
        try:
            event.ownership_signals = infer_ownership(event, workspace)
        except Exception:
            pass

    # Stage 6: drift detection (reads issue_memory_signals — must be after stage 4)
    for event in enriched:
        try:
            event.drift_signals = detect_drift(event)
        except Exception:
            pass

    # Stage 7: graph construction + attach signals to events before ranking
    graph, graph_signals = build_issue_graph(enriched)
    for event in enriched:
        sig = graph_signals.get(event.event_id)
        if sig is not None:
            event.graph_signals = sig

    # Build per-event traces after all signals are attached
    traces = {e.event_id: _build_trace(e, now) for e in enriched}

    return {
        "enriched": enriched,
        "embedding_store": embedding_store,
        "clusters": clusters,
        "graph": graph,
        "graph_signals": graph_signals,
        "traces": traces,
    }


def _inject_thread_reply(r: dict) -> None:
    """Inject a reply dict into workspace in-memory objects."""
    ts = datetime.fromisoformat(r["timestamp"])
    msg = RawSlackMessage(
        message_id=r["message_id"],
        thread_id=r["thread_id"],
        channel_id=r["channel_id"],
        user_id=r["user_id"],
        text=r["text"],
        timestamp=ts,
        is_thread_root=False,
    )
    _workspace.messages.append(msg)

    thread = next(
        (t for t in _workspace.threads if t.thread_id == r["thread_id"]), None
    )
    if thread:
        thread.reply_count += 1
        if ts > thread.last_activity_at:
            thread.last_activity_at = ts
        if r["user_id"] not in thread.participant_ids:
            thread.participant_ids.append(r["user_id"])
        if r["message_id"] not in thread.message_ids:
            thread.message_ids.append(r["message_id"])

    # Update root message reply_count for display
    root = next(
        (m for m in _workspace.messages if m.message_id == r["thread_id"]), None
    )
    if root:
        root.reply_count += 1


async def digest_refresh_worker() -> None:
    """Background task: re-runs the full pipeline when new thread replies arrive."""
    global _needs_refresh, _digests, _profiles, _enriched_events, _embedding_store, _cached_graph, _event_traces, _workspace
    while True:
        await asyncio.sleep(10)
        if not _needs_refresh:
            continue
        _needs_refresh = False
        await asyncio.sleep(3)  # debounce: collect burst of replies
        try:
            now = datetime.now(tz=timezone.utc)
            # In Slack-integrated mode, reload the workspace from the ingest store
            # so reconciled thread data is picked up. In local/demo mode, keep the
            # existing _workspace — it already has in-memory mutations from
            # _inject_thread_reply and reloading from disk would discard them.
            bot_token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
            if bot_token:
                fresh_workspace = _load_workspace()
                _workspace = fresh_workspace
            else:
                fresh_workspace = _workspace

            result = _run_pipeline(fresh_workspace, now)
            enriched = result["enriched"]
            embedding_store = result["embedding_store"]

            _enriched_events = enriched
            _embedding_store = embedding_store
            _cached_graph = result["graph"]
            _event_traces = result["traces"]

            _profiles = build_user_profiles(fresh_workspace, enriched, now=now)
            events_by_id = {e.event_id: e for e in enriched}
            shared = build_shared_summaries(events_by_id, [e.event_id for e in enriched])

            for user in fresh_workspace.users:
                uid = user.user_id
                if uid not in _profiles:
                    continue
                digest = assemble_digest(
                    user_id=uid,
                    enriched_events=enriched,
                    profile=_profiles[uid],
                    events_by_id=events_by_id,
                    top_k=5,
                    now=now,
                    date_str=now.strftime("%Y-%m-%d"),
                    embedding_store=embedding_store,
                    include_excluded=True,
                    shared_summaries=shared,
                )
                _digests[uid] = digest
                for item in digest.items:
                    if item.event_id in _event_traces:
                        _event_traces[item.event_id].final_score = item.score
            logger.info("[digest refresh] completed at %s — %d digests updated", now.isoformat(), len(_digests))
        except Exception as e:
            logger.error("[digest refresh] failed: %s", e, exc_info=True)


@app.on_event("startup")
async def startup():
    """
    Full pipeline startup in correct dependency order:
    ingest → enrich → issue linking → issue memory → ownership → drift →
    graph (graph_signals attached before ranking) → profiles → digests.

    Persisted replies are injected into the workspace before the pipeline runs.
    """
    global _workspace, _digests, _profiles, _enriched_events, _embedding_store, _cached_graph, _event_traces

    _workspace = _load_workspace()

    # Restore persisted state
    init_db()

    persisted_dms = load_dm_messages()
    for key, msgs in persisted_dms.items():
        _dm_messages[key].extend(msgs)

    for r in load_thread_replies():
        _inject_thread_reply(r)

    # Run the full pipeline (all stages in correct order)
    result = _run_pipeline(_workspace, NOW)
    enriched = result["enriched"]
    embedding_store = result["embedding_store"]

    # Cache pipeline outputs for use by graph/shared-context endpoints
    _enriched_events = enriched
    _embedding_store = embedding_store
    _cached_graph = result["graph"]
    _event_traces = result["traces"]

    # Stage 8: user profiles (needs enriched events with all signals attached)
    _profiles = build_user_profiles(_workspace, enriched, now=NOW)

    events_by_id = {e.event_id: e for e in enriched}
    all_event_ids = [e.event_id for e in enriched]
    shared = build_shared_summaries(events_by_id, all_event_ids)

    for user in _workspace.users:
        uid = user.user_id
        if uid not in _profiles:
            continue
        digest = assemble_digest(
            user_id=uid,
            enriched_events=enriched,
            profile=_profiles[uid],
            events_by_id=events_by_id,
            top_k=5,
            now=NOW,
            date_str="2026-04-10",
            embedding_store=embedding_store,
            include_excluded=True,
            shared_summaries=shared,
        )
        _digests[uid] = digest
        # Backfill final scores into traces using this user's actual ranked scores
        for item in digest.items:
            if item.event_id in _event_traces:
                _event_traces[item.event_id].final_score = item.score

    asyncio.create_task(digest_refresh_worker())

    # Start Slack integration if bot token is configured
    bot_token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if bot_token:
        try:
            from api.slack_events import get_store, get_limiter, get_metrics
            slack_store = get_store()
            slack_limiter = get_limiter()
            slack_metrics_obj = get_metrics()

            global _reconciler, _socket_mode_manager

            _reconciler = ReconciliationWorker(
                store=slack_store,
                limiter=slack_limiter,
                bot_token=bot_token,
                metrics=slack_metrics_obj,
                refresh_callback=_trigger_refresh,
            )
            asyncio.create_task(_reconciler.run())
            logger.info("[server] ReconciliationWorker started")

            _socket_mode_manager = SocketModeManager(
                store=slack_store,
                limiter=slack_limiter,
                metrics=slack_metrics_obj,
            )
            if _socket_mode_manager.is_configured():
                asyncio.create_task(_socket_mode_manager.start())
                logger.info("[server] SocketModeManager started")
        except Exception as e:
            logger.warning("[server] Slack integration startup failed: %s — continuing without it", e)


@app.get("/api/workspace")
def get_workspace():
    if _workspace is None:
        raise HTTPException(status_code=503, detail="Workspace not loaded")
    return {
        "users": [u.model_dump() for u in _workspace.users],
        "channels": [c.model_dump() for c in _workspace.channels],
    }


@app.get("/api/channels/{channel_id}/messages")
def get_channel_messages(channel_id: str):
    if _workspace is None:
        raise HTTPException(status_code=503, detail="Workspace not loaded")

    channel = next((c for c in _workspace.channels if c.channel_id == channel_id), None)
    if channel is None:
        raise HTTPException(status_code=404, detail=f"Channel {channel_id} not found")

    user_map = {u.user_id: u.display_name for u in _workspace.users}

    messages = [m for m in _workspace.messages if m.channel_id == channel_id]
    messages.sort(key=lambda m: m.timestamp)

    return {
        "channel_id": channel_id,
        "name": channel.name,
        "topic": channel.topic,
        "messages": [
            {
                **m.model_dump(mode="json"),
                "display_name": user_map.get(m.user_id, m.user_id),
            }
            for m in messages
        ],
    }


@app.get("/api/threads/{thread_id}")
def get_thread(thread_id: str):
    if _workspace is None:
        raise HTTPException(status_code=503, detail="Workspace not loaded")

    thread = next((t for t in _workspace.threads if t.thread_id == thread_id), None)
    if thread is None:
        raise HTTPException(status_code=404, detail=f"Thread {thread_id} not found")

    user_map = {u.user_id: u.display_name for u in _workspace.users}
    channel = next(
        (c for c in _workspace.channels if c.channel_id == thread.channel_id), None
    )

    messages = [m for m in _workspace.messages if m.thread_id == thread_id]
    messages.sort(key=lambda m: m.timestamp)

    return {
        "thread_id": thread_id,
        "channel_id": thread.channel_id,
        "channel_name": channel.name if channel else thread.channel_id,
        "started_at": thread.started_at.isoformat(),
        "last_activity_at": thread.last_activity_at.isoformat(),
        "messages": [
            {
                **m.model_dump(mode="json"),
                "display_name": user_map.get(m.user_id, m.user_id),
            }
            for m in messages
        ],
    }


@app.post("/api/threads/{thread_id}/reply")
def post_thread_reply(
    thread_id: str,
    as_: str = Query(..., alias="as"),
    body: dict = Body(...),
):
    global _needs_refresh

    if _workspace is None:
        raise HTTPException(status_code=503, detail="Workspace not loaded")

    thread = next((t for t in _workspace.threads if t.thread_id == thread_id), None)
    if thread is None:
        raise HTTPException(status_code=404, detail=f"Thread {thread_id} not found")

    user_map = {u.user_id: u.display_name for u in _workspace.users}
    now = datetime.now(tz=timezone.utc)

    r = {
        "message_id": str(uuid.uuid4()),
        "thread_id": thread_id,
        "channel_id": thread.channel_id,
        "user_id": as_,
        "text": body.get("text", ""),
        "timestamp": now.isoformat(),
    }

    _inject_thread_reply(r)
    save_thread_reply(r)
    _needs_refresh = True

    return {
        **r,
        "display_name": user_map.get(as_, as_),
        "is_thread_root": False,
        "reaction_counts": {},
        "reply_count": 0,
        "mentions": [],
    }


@app.get("/api/digest/{user_id}")
def get_digest(user_id: str):
    if user_id not in _digests:
        raise HTTPException(status_code=404, detail=f"No digest for user {user_id}")
    return _digests[user_id].model_dump(mode="json")


@app.get("/api/digest/{user_id}/debug")
def get_digest_debug(user_id: str):
    """Same as /api/digest/{user_id} but explicitly includes excluded_items."""
    if user_id not in _digests:
        raise HTTPException(status_code=404, detail=f"No digest for user {user_id}")
    return _digests[user_id].model_dump(mode="json")


@app.get("/api/users/{user_id}/profile")
def get_user_profile(user_id: str):
    if user_id not in _profiles:
        raise HTTPException(status_code=404, detail=f"No profile for user {user_id}")
    return _profiles[user_id].model_dump(mode="json")


@app.get("/api/dm/{other_user_id}")
def get_dm(other_user_id: str, as_: str = Query(..., alias="as")):
    return {"messages": _dm_messages[_dm_key(as_, other_user_id)]}


@app.post("/api/dm/{other_user_id}")
def post_dm(
    other_user_id: str,
    as_: str = Query(..., alias="as"),
    body: dict = Body(...),
):
    msg = {
        "message_id": str(uuid.uuid4()),
        "sender_id": as_,
        "text": body.get("text", ""),
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
    }
    _dm_messages[_dm_key(as_, other_user_id)].append(msg)
    save_dm_message(as_, other_user_id, msg)
    return msg


@app.get("/api/graph")
def get_graph(user_id: Optional[str] = None):
    """Return graph nodes and edges from the cached pipeline output."""
    if _workspace is None:
        raise HTTPException(status_code=503, detail="Workspace not loaded")
    if _cached_graph is None:
        return {"nodes": [], "edges": [], "error": "Graph not yet computed"}
    return _cached_graph.model_dump(mode="json")


@app.get("/api/shared-context")
def get_shared_context(user_id: Optional[str] = None):
    """Return org-wide shared context and misalignment signals from cached events."""
    if _workspace is None:
        raise HTTPException(status_code=503, detail="Workspace not loaded")

    from src.digest.shared_context import build_shared_context

    try:
        view = build_shared_context(_enriched_events, _profiles)
        return view.model_dump(mode="json")
    except Exception as e:
        return {"globally_critical": [], "cross_functional_hotspots": [], "misalignments": [], "error": str(e)}


@app.get("/api/events/{event_id}/ownership")
def get_event_ownership(event_id: str):
    """Return ownership signals for a specific event (from cached enriched events)."""
    if _workspace is None:
        raise HTTPException(status_code=503, detail="Workspace not loaded")

    event = next((e for e in _enriched_events if e.event_id == event_id), None)
    if event is None:
        raise HTTPException(status_code=404, detail=f"Event {event_id} not found")
    if event.ownership_signals is None:
        raise HTTPException(status_code=404, detail=f"No ownership signals for event {event_id}")
    return event.ownership_signals.model_dump(mode="json")


@app.get("/api/events/{event_id}/trace")
def get_event_trace(event_id: str):
    """Return the full pipeline trace for a specific event."""
    trace = _event_traces.get(event_id)
    if trace is None:
        raise HTTPException(status_code=404, detail=f"No trace found for event {event_id}")
    return trace.model_dump(mode="json")


@app.get("/api/traces")
def list_traces():
    """Return a summary of all available event traces."""
    return [
        {
            "event_id": t.event_id,
            "thread_id": t.thread_id,
            "channel_id": t.channel_id,
            "title": next(
                (s.outputs.get("title", t.event_id) for s in t.stages if s.name == "enrichment"), t.event_id
            ),
            "final_score": t.final_score,
            "top_driver": t.top_driver,
        }
        for t in _event_traces.values()
    ]


@app.get("/health")
def health():
    return {"status": "ok", "digests_ready": len(_digests) > 0}


@app.get("/api/slack/status")
def slack_status():
    """Return Slack integration health and dirty queue state."""
    bot_token_set = bool(os.environ.get("SLACK_BOT_TOKEN", "").strip())
    app_token_set = bool(os.environ.get("SLACK_APP_TOKEN", "").strip())
    signing_secret_set = bool(os.environ.get("SLACK_SIGNING_SECRET", "").strip())

    reconciler_running = _reconciler is not None and _reconciler._running
    socket_mode_running = (
        _socket_mode_manager is not None
        and _socket_mode_manager._running
        and _socket_mode_manager.is_configured()
    )

    # Get dirty thread count from slack store if available
    dirty_count = 0
    store_stats: dict = {}
    try:
        from api.slack_events import get_store
        store = get_store()
        dirty_count = len(store.get_dirty_threads(limit=1000))
        store_stats = store.stats()
    except Exception:
        pass

    return {
        "slack_configured": bot_token_set,
        "signing_secret_configured": signing_secret_set,
        "socket_mode_configured": app_token_set,
        "reconciler_running": reconciler_running,
        "socket_mode_running": socket_mode_running,
        "dirty_threads_pending": dirty_count,
        "store": store_stats,
        "digests_ready": len(_digests) > 0,
        "refresh_pending": _needs_refresh,
    }
