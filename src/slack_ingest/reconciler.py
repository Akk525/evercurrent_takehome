"""
Targeted reconciliation worker.

Purpose:
    Fill gaps in local thread data that event-driven ingestion cannot cover:
    - Initial channel backfill (first run, or after downtime)
    - Fetch replies for dirty threads (marked dirty by event processing)
    - Recover missing thread history after connectivity gaps

This is NOT the primary ingestion path. Events API / Socket Mode handles that.
The reconciler is a targeted repair mechanism that runs conservatively.

Design:
    - Processes at most `max_threads_per_run` dirty threads per cycle
    - Respects rate-limit buckets for conversations.history and conversations.replies
    - Backs off on 429 and waits for Retry-After
    - Runs as an async background task (started alongside the FastAPI server)
    - Tracks per-channel backfill cursors in the store (last_known_ts)

Key constraint:
    conversations.history and conversations.replies are Tier 1 methods — 1 RPM.
    The reconciler must never use them in a tight loop.
    10 dirty threads = at least 10 minutes of clock time at minimum.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from .rate_limits import RateLimiter, SlackMethod, parse_retry_after, SlackApiError
from .store import SlackIngestStore, StoredMessage, StoredThread
from .events import _extract_mentions

import json

logger = logging.getLogger(__name__)


class ReconciliationWorker:
    """
    Async background worker for targeted Slack reconciliation.

    Usage (in FastAPI startup):
        worker = ReconciliationWorker(store, limiter, bot_token)
        asyncio.create_task(worker.run())
    """

    def __init__(
        self,
        store: SlackIngestStore,
        limiter: RateLimiter,
        bot_token: str,
        poll_interval_seconds: float = 60.0,
        max_threads_per_run: int = 5,
        metrics=None,  # Optional[SlackIngestMetrics]
        refresh_callback=None,  # Optional[Callable] — called after any thread is cleaned
    ):
        self.store = store
        self.limiter = limiter
        self.bot_token = bot_token
        self.poll_interval = poll_interval_seconds
        self.max_threads_per_run = max_threads_per_run
        self.metrics = metrics
        self.refresh_callback = refresh_callback
        self._running = False

    async def run(self) -> None:
        """Main loop — runs forever until stopped."""
        self._running = True
        logger.info(
            "[reconciler] Started. Poll interval: %.0fs, max threads/run: %d",
            self.poll_interval,
            self.max_threads_per_run,
        )
        while self._running:
            try:
                await self._reconcile_cycle()
            except Exception as e:
                logger.error("[reconciler] Unexpected error: %s", e, exc_info=True)
            await asyncio.sleep(self.poll_interval)

    def stop(self) -> None:
        self._running = False

    async def _reconcile_cycle(self) -> None:
        """One reconciliation pass: fetch replies for up to N dirty threads."""
        dirty = self.store.get_dirty_threads(limit=self.max_threads_per_run)
        if not dirty:
            logger.debug("[reconciler] No dirty threads — nothing to do")
            return

        logger.info("[reconciler] %d dirty thread(s) to reconcile", len(dirty))
        for thread_id, channel_id in dirty:
            await self._reconcile_thread(thread_id, channel_id)

    async def _reconcile_thread(self, thread_id: str, channel_id: str) -> None:
        """
        Fetch all replies for a dirty thread using conversations.replies.

        Rate-limit aware: acquires a Tier 1 token before each API call.
        On 429: applies Retry-After and gives up for this cycle (will retry next run).
        """
        logger.info("[reconciler] Fetching replies for thread %s in %s", thread_id, channel_id)

        # Acquire rate-limit token — this may wait up to 120s
        acquired = await self.limiter.acquire(
            SlackMethod.CONVERSATIONS_REPLIES, timeout=120.0
        )
        if not acquired:
            logger.warning(
                "[reconciler] Rate-limit token timeout for %s — deferring thread %s",
                SlackMethod.CONVERSATIONS_REPLIES.value,
                thread_id,
            )
            return

        try:
            messages = await self._fetch_thread_replies(thread_id, channel_id)
            if messages is None:
                return  # 429 — deferred

            # Update store with fetched messages
            for msg_data in messages:
                ts = msg_data.get("ts", "")
                if not ts:
                    continue
                user = msg_data.get("user", "") or msg_data.get("bot_id", "")
                text = msg_data.get("text", "")
                msg_thread_ts = msg_data.get("thread_ts", ts)
                is_root = (msg_thread_ts == ts)
                timestamp_dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)

                stored = StoredMessage(
                    message_id=ts,
                    thread_id=msg_thread_ts,
                    channel_id=channel_id,
                    user_id=user,
                    text=text,
                    timestamp=timestamp_dt.isoformat(),
                    is_thread_root=is_root,
                    is_deleted=False,
                    is_edited=False,
                    reaction_counts=json.dumps(msg_data.get("reactions", {})),
                    mentions=json.dumps(_extract_mentions(text)),
                )
                self.store.upsert_message(stored)

            self.store.mark_thread_clean(thread_id)
            if self.metrics:
                self.metrics.reconciliation_reads += 1
                self.metrics.reconciliation_successes += 1
            logger.info(
                "[reconciler] Thread %s reconciled (%d messages fetched)",
                thread_id,
                len(messages),
            )
            if self.refresh_callback is not None:
                try:
                    self.refresh_callback()
                except Exception as e:
                    logger.warning("[reconciler] refresh_callback failed: %s", e)

        except SlackApiError as e:
            logger.error(
                "[reconciler] Slack API error reconciling thread %s: %s",
                thread_id,
                e,
            )

    async def _fetch_thread_replies(
        self,
        thread_id: str,
        channel_id: str,
    ) -> Optional[list[dict]]:
        """
        Call conversations.replies for the given thread.

        Returns list of message dicts, or None if rate-limited.
        Raises SlackApiError on other failures.
        """
        try:
            from slack_sdk.web.async_client import AsyncWebClient
            from slack_sdk.errors import SlackApiError as SdkApiError
        except ImportError:
            logger.warning("[reconciler] slack_sdk not installed — reconciliation skipped")
            return None

        client = AsyncWebClient(token=self.bot_token)
        try:
            response = await client.conversations_replies(
                channel=channel_id,
                ts=thread_id,
                limit=200,
            )
            return response.get("messages", [])

        except SdkApiError as e:
            status = getattr(e.response, "status_code", 0)
            if status == 429:
                retry_after = parse_retry_after(dict(e.response.headers))
                self.limiter.handle_429(SlackMethod.CONVERSATIONS_REPLIES, retry_after)
                if self.metrics:
                    self.metrics.rate_limit_hits += 1
                logger.warning(
                    "[reconciler] 429 on conversations.replies — Retry-After %.0fs",
                    retry_after,
                )
                return None
            raise SlackApiError(
                method=SlackMethod.CONVERSATIONS_REPLIES.value,
                error=str(e),
                status_code=status,
            )

    async def backfill_channel(
        self,
        channel_id: str,
        oldest: Optional[str] = None,
        limit: int = 200,
    ) -> int:
        """
        Initial backfill for a channel using conversations.history.

        ONLY call this on first setup or after a downtime gap.
        Not for routine use — conversations.history is Tier 1 (1 RPM).

        Returns the number of messages fetched.
        """
        logger.info(
            "[reconciler] Backfilling channel %s (oldest=%s, limit=%d)",
            channel_id, oldest, limit,
        )

        acquired = await self.limiter.acquire(
            SlackMethod.CONVERSATIONS_HISTORY, timeout=120.0
        )
        if not acquired:
            logger.warning("[reconciler] Rate-limit timeout for conversations.history — aborting backfill")
            return 0

        try:
            from slack_sdk.web.async_client import AsyncWebClient
            from slack_sdk.errors import SlackApiError as SdkApiError
        except ImportError:
            logger.warning("[reconciler] slack_sdk not installed — backfill skipped")
            return 0

        client = AsyncWebClient(token=self.bot_token)
        try:
            kwargs: dict = {"channel": channel_id, "limit": limit}
            if oldest:
                kwargs["oldest"] = oldest

            response = await client.conversations_history(**kwargs)
            messages = response.get("messages", [])

            for msg_data in messages:
                ts = msg_data.get("ts", "")
                if not ts:
                    continue
                user = msg_data.get("user", "") or msg_data.get("bot_id", "")
                text = msg_data.get("text", "")
                thread_ts = msg_data.get("thread_ts", ts)
                reply_count = msg_data.get("reply_count", 0)
                timestamp_dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)

                stored_msg = StoredMessage(
                    message_id=ts,
                    thread_id=thread_ts,
                    channel_id=channel_id,
                    user_id=user,
                    text=text,
                    timestamp=timestamp_dt.isoformat(),
                    is_thread_root=(thread_ts == ts),
                    is_deleted=False,
                    is_edited=False,
                    reaction_counts="{}",
                    mentions=json.dumps(_extract_mentions(text)),
                )
                self.store.upsert_message(stored_msg)

                # If thread has replies, create a dirty thread record
                if reply_count > 0:
                    stored_thread = StoredThread(
                        thread_id=thread_ts,
                        channel_id=channel_id,
                        root_message_id=ts,
                        participant_ids=json.dumps([user] if user else []),
                        message_ids=json.dumps([ts]),
                        started_at=timestamp_dt.isoformat(),
                        last_activity_at=timestamp_dt.isoformat(),
                        reply_count=reply_count,
                        is_dirty=True,    # Mark dirty — replies not yet fetched
                        is_complete=False,
                    )
                    self.store.upsert_thread(stored_thread)

            # Update channel backfill cursor
            if messages:
                latest_ts = messages[0].get("ts", "")
                if latest_ts:
                    self.store.update_channel_cursor(channel_id, latest_ts)

            if self.metrics:
                self.metrics.reconciliation_reads += 1

            logger.info("[reconciler] Backfilled %d messages from %s", len(messages), channel_id)
            return len(messages)

        except SdkApiError as e:
            status = getattr(e.response, "status_code", 0)
            if status == 429:
                retry_after = parse_retry_after(dict(e.response.headers))
                self.limiter.handle_429(SlackMethod.CONVERSATIONS_HISTORY, retry_after)
                if self.metrics:
                    self.metrics.rate_limit_hits += 1
                logger.warning("[reconciler] 429 on conversations.history — Retry-After %.0fs", retry_after)
                return 0
            logger.error("[reconciler] conversations.history error: %s", e)
            return 0
