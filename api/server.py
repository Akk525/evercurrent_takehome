"""
FastAPI server wrapping the digest engine for the Slack-like demo UI.

Serves:
- Workspace data (users, channels)
- Channel message feeds
- Thread details
- Per-user digests (pre-computed at startup)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from src.ingest import load_workspace
from src.events import build_candidate_events
from src.enrichment import enrich_candidate_events
from src.profiles import build_user_profiles
from src.digest.assembler import assemble_digest
from src.summarization import build_shared_summaries
from src.models.raw import SlackWorkspace

DATA_DIR = Path(__file__).parent.parent / "data" / "mock_slack"
NOW = datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc)

app = FastAPI(title="Digest Engine API")

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


@app.on_event("startup")
async def startup():
    """
    Single-pass startup: ingest → enrich → profile → shared summaries → per-user digests.
    All stages share the same enriched corpus — no redundant computation.
    """
    global _workspace, _digests, _profiles
    _workspace = load_workspace(DATA_DIR)

    events = build_candidate_events(_workspace)
    enriched = enrich_candidate_events(events, _workspace, now=NOW)
    _profiles = build_user_profiles(_workspace, enriched, now=NOW)

    events_by_id = {e.event_id: e for e in enriched}
    all_event_ids = [e.event_id for e in enriched]
    shared = build_shared_summaries(events_by_id, all_event_ids)

    # Build embedding store once for ranking
    from src.enrichment.enricher import _build_embedding_store
    embedding_store = _build_embedding_store(enriched)

    for user in _workspace.users:
        uid = user.user_id
        if uid not in _profiles:
            continue
        _digests[uid] = assemble_digest(
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

    messages = [
        m for m in _workspace.messages if m.channel_id == channel_id
    ]
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
    channel = next((c for c in _workspace.channels if c.channel_id == thread.channel_id), None)

    messages = [
        m for m in _workspace.messages if m.thread_id == thread_id
    ]
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


@app.get("/health")
def health():
    return {"status": "ok", "digests_ready": len(_digests) > 0}
