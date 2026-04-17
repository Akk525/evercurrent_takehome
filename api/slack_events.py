"""
FastAPI router for Slack Events API HTTP endpoint.

Mounts at: /slack/events
Handles:
  - GET  /slack/events/health  — liveness check (no Slack token required)
  - POST /slack/events         — Slack event delivery + URL verification challenge
  - GET  /slack/metrics        — ingest + rate-limit metrics snapshot

Signature verification:
  Enabled when SLACK_SIGNING_SECRET is set.
  Disabled (with a warning) when absent — useful for local dev with ngrok.

Integration with existing server:
  Mount this router in api/server.py:
      from api.slack_events import router as slack_router
      app.include_router(slack_router)

Required env vars for full operation:
  SLACK_SIGNING_SECRET  — for request verification
  SLACK_BOT_TOKEN       — for reconciler API calls
  SLACK_APP_TOKEN       — for Socket Mode (alternative to HTTP endpoint)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response

from src.slack_ingest.store import SlackIngestStore
from src.slack_ingest.rate_limits import RateLimiter
from src.slack_ingest.events import process_slack_event
from src.slack_ingest.http_events import verify_slack_signature, get_signing_secret
from src.slack_ingest.models import SlackEventEnvelope
from src.observability.slack_metrics import SlackIngestMetrics

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/slack", tags=["slack"])

# Module-level singletons — initialized lazily on first request or at app startup
_store: Optional[SlackIngestStore] = None
_limiter: Optional[RateLimiter] = None
_metrics: Optional[SlackIngestMetrics] = None

_DB_PATH = Path(__file__).parent.parent / "data" / "slack_ingest.db"


def get_store() -> SlackIngestStore:
    global _store
    if _store is None:
        _store = SlackIngestStore(db_path=_DB_PATH)
        _store.init()
    return _store


def get_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter()
    return _limiter


def get_metrics() -> SlackIngestMetrics:
    global _metrics
    if _metrics is None:
        _metrics = SlackIngestMetrics()
    return _metrics


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/events/health")
def health():
    """Liveness check — no Slack credentials required."""
    store = get_store()
    return {
        "status": "ok",
        "signing_secret_configured": get_signing_secret() is not None,
        "store_stats": store.stats(),
    }


@router.post("/events")
async def receive_slack_event(request: Request) -> dict:
    """
    Receive and process Slack Events API payloads.

    Immediately acknowledges all valid requests (Slack requires < 3s response).
    Actual event processing is dispatched as a background task.
    """
    body = await request.body()

    # Signature verification
    signing_secret = get_signing_secret()
    if signing_secret:
        timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
        signature = request.headers.get("X-Slack-Signature", "")
        if not verify_slack_signature(body, timestamp, signature, signing_secret):
            logger.warning("[slack_events] Signature verification failed")
            raise HTTPException(status_code=403, detail="Invalid Slack signature")
    else:
        logger.warning(
            "[slack_events] SLACK_SIGNING_SECRET not set — skipping signature verification. "
            "Set it in production."
        )

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # URL verification challenge (initial handshake)
    if payload.get("type") == "url_verification":
        challenge = payload.get("challenge", "")
        logger.info("[slack_events] URL verification challenge received")
        return {"challenge": challenge}

    # Parse envelope and process in background
    try:
        envelope = SlackEventEnvelope.model_validate(payload)
    except Exception as e:
        logger.warning("[slack_events] Could not parse envelope: %s", e)
        # Return 200 anyway — Slack will retry if we return an error
        return {"ok": True}

    # Fire-and-forget processing — return immediately
    asyncio.create_task(_process_event_background(envelope))

    return {"ok": True}


async def _process_event_background(envelope: SlackEventEnvelope) -> None:
    """Process a Slack event in the background (after HTTP response is sent)."""
    try:
        store = get_store()
        metrics = get_metrics()
        process_slack_event(envelope, store, metrics)
    except Exception as e:
        logger.error("[slack_events] Background event processing failed: %s", e, exc_info=True)


@router.get("/metrics")
def slack_metrics():
    """Return Slack integration metrics snapshot."""
    metrics = get_metrics()
    limiter = get_limiter()
    store = get_store()
    return {
        "ingest": metrics.to_dict(),
        "rate_limits": limiter.metrics(),
        "store": store.stats(),
    }


@router.get("/dirty-threads")
def get_dirty_threads(limit: int = 50):
    """
    Return the current dirty thread queue — threads with unreconciled replies.

    Useful for debugging and monitoring reconciliation backlog.
    """
    store = get_store()
    dirty = store.get_dirty_threads(limit=limit)
    return {
        "count": len(dirty),
        "threads": [{"thread_id": tid, "channel_id": cid} for tid, cid in dirty],
        "limit": limit,
    }


@router.post("/reconcile/{channel_id}")
async def trigger_reconcile(channel_id: str):
    """
    Manually trigger a channel backfill (admin / debug endpoint).

    Performs one conversations.history read for the channel.
    Only use during initial setup or after downtime.
    """
    bot_token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if not bot_token:
        raise HTTPException(status_code=503, detail="SLACK_BOT_TOKEN not configured")

    from src.slack_ingest.reconciler import ReconciliationWorker
    store = get_store()
    limiter = get_limiter()
    metrics = get_metrics()

    worker = ReconciliationWorker(
        store=store,
        limiter=limiter,
        bot_token=bot_token,
        metrics=metrics,
    )

    channel = store.get_channel(channel_id)
    oldest = channel.last_known_ts if channel else None

    count = await worker.backfill_channel(channel_id, oldest=oldest)
    return {
        "channel_id": channel_id,
        "messages_fetched": count,
        "store_stats": store.stats(),
    }
