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

# In-memory DM store: canonical key = "uid_a:uid_b" (sorted)
_dm_messages: dict[str, list[dict]] = defaultdict(list)

# Digest refresh flag: set True when thread replies arrive
_needs_refresh: bool = False


def _dm_key(a: str, b: str) -> str:
    return ":".join(sorted([a, b]))


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
    global _needs_refresh, _digests, _profiles
    while True:
        await asyncio.sleep(10)
        if not _needs_refresh:
            continue
        _needs_refresh = False
        await asyncio.sleep(3)  # debounce: collect burst of replies
        try:
            now = datetime.now(tz=timezone.utc)
            events = build_candidate_events(_workspace)
            enriched = enrich_candidate_events(events, _workspace, now=now)
            _profiles = build_user_profiles(_workspace, enriched, now=now)
            events_by_id = {e.event_id: e for e in enriched}
            shared = build_shared_summaries(events_by_id, [e.event_id for e in enriched])
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
                    now=now,
                    date_str=now.strftime("%Y-%m-%d"),
                    embedding_store=embedding_store,
                    include_excluded=True,
                    shared_summaries=shared,
                )
            print(f"[digest refresh] completed at {now.isoformat()}")
        except Exception as e:
            print(f"[digest refresh] failed: {e}")


@app.on_event("startup")
async def startup():
    """
    Single-pass startup: ingest → enrich → profile → shared summaries → per-user digests.
    Persisted replies are injected into the workspace before the pipeline runs.
    """
    global _workspace, _digests, _profiles
    _workspace = load_workspace(DATA_DIR)

    # Restore persisted state
    init_db()

    persisted_dms = load_dm_messages()
    for key, msgs in persisted_dms.items():
        _dm_messages[key].extend(msgs)

    for r in load_thread_replies():
        _inject_thread_reply(r)

    # Run pipeline on (possibly enriched) workspace
    events = build_candidate_events(_workspace)
    enriched = enrich_candidate_events(events, _workspace, now=NOW)
    _profiles = build_user_profiles(_workspace, enriched, now=NOW)

    events_by_id = {e.event_id: e for e in enriched}
    all_event_ids = [e.event_id for e in enriched]
    shared = build_shared_summaries(events_by_id, all_event_ids)

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

    asyncio.create_task(digest_refresh_worker())


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


@app.get("/health")
def health():
    return {"status": "ok", "digests_ready": len(_digests) > 0}
