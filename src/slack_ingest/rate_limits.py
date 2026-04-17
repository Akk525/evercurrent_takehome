"""
Rate-limit-aware Slack API client wrapper.

Implements:
  1. Per-method token buckets (conservative, aligned with Slack's published tiers)
  2. 429 / Retry-After detection and automatic back-off
  3. app_rate_limited event tracking
  4. Logging of every limit event for observability

Slack API tiers (approximate, for internal/single-workspace apps):
  Tier 1: ~1  RPM — conversations.history, conversations.replies
  Tier 2: ~20 RPM — channels.info, users.info, users.list
  Tier 3: ~50 RPM — chat.postMessage
  Tier 4: ~100 RPM — other methods

References:
  https://api.slack.com/docs/rate-limits
  https://api.slack.com/changelog/2025-05-terms-rate-limit-update-and-faq

Design principle:
  The reconciler MUST acquire a rate-limit token before every Slack API read.
  If no token is available, the call is deferred — not skipped and not spammed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Method registry — Slack Tier assignments
# ---------------------------------------------------------------------------

class SlackMethod(str, Enum):
    """Known Slack API methods and their rate-limit tier."""
    CONVERSATIONS_HISTORY = "conversations.history"
    CONVERSATIONS_REPLIES = "conversations.replies"
    CONVERSATIONS_LIST    = "conversations.list"
    CHANNELS_INFO         = "channels.info"
    USERS_INFO            = "users.info"
    USERS_LIST            = "users.list"
    CHAT_POST_MESSAGE     = "chat.postMessage"
    CHAT_POST_EPHEMERAL   = "chat.postEphemeral"
    AUTH_TEST             = "auth.test"


# Tokens per minute for each method — conservative relative to published tiers
# so we never hit the limit in normal operation.
_METHOD_RPM: dict[SlackMethod, float] = {
    SlackMethod.CONVERSATIONS_HISTORY: 1.0,   # Tier 1 — very restrictive
    SlackMethod.CONVERSATIONS_REPLIES: 1.0,   # Tier 1 — very restrictive
    SlackMethod.CONVERSATIONS_LIST:    10.0,  # Tier 2 (conservative)
    SlackMethod.CHANNELS_INFO:         15.0,  # Tier 2
    SlackMethod.USERS_INFO:            15.0,  # Tier 2
    SlackMethod.USERS_LIST:            10.0,  # Tier 2 (conservative)
    SlackMethod.CHAT_POST_MESSAGE:     40.0,  # Tier 3 (conservative)
    SlackMethod.CHAT_POST_EPHEMERAL:   40.0,  # Tier 3
    SlackMethod.AUTH_TEST:             20.0,  # Tier 2
}


# ---------------------------------------------------------------------------
# Token bucket
# ---------------------------------------------------------------------------

@dataclass
class _TokenBucket:
    """
    Token bucket for a single Slack API method.

    Capacity = 1 token (intentionally minimal — we're not trying to saturate Slack).
    Refills at `rpm / 60.0` tokens per second.
    """
    method: SlackMethod
    rpm: float
    _tokens: float = field(init=False)
    _last_refill: float = field(init=False)
    # When a 429 was received: blocked until this monotonic time
    _blocked_until: float = field(init=False, default=0.0)

    def __post_init__(self) -> None:
        self._tokens = 1.0  # Start with one token
        self._last_refill = time.monotonic()

    @property
    def refill_rate(self) -> float:
        """Tokens per second."""
        return self.rpm / 60.0

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(1.0, self._tokens + elapsed * self.refill_rate)
        self._last_refill = now

    def blocked_seconds(self) -> float:
        """How many seconds until this method is unblocked (0 = not blocked)."""
        remaining = self._blocked_until - time.monotonic()
        return max(0.0, remaining)

    def apply_retry_after(self, retry_after_seconds: float) -> None:
        """Called when a 429 response is received with a Retry-After header."""
        self._blocked_until = time.monotonic() + retry_after_seconds
        self._tokens = 0.0
        logger.warning(
            "[rate_limit] %s blocked for %.1fs (Retry-After)",
            self.method.value,
            retry_after_seconds,
        )

    def try_acquire(self) -> bool:
        """
        Try to consume one token. Returns True if a token was available.
        Does not block — callers decide what to do on False.
        """
        if self.blocked_seconds() > 0:
            return False
        self._refill()
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False

    async def acquire(self, timeout: float = 120.0) -> bool:
        """
        Async wait until a token is available, up to `timeout` seconds.
        Returns True if acquired, False if timed out.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            blocked = self.blocked_seconds()
            if blocked > 0:
                wait = min(blocked, deadline - time.monotonic(), 5.0)
                logger.debug("[rate_limit] %s blocked for %.1fs, sleeping", self.method.value, wait)
                await asyncio.sleep(wait)
                continue

            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True

            # Not enough tokens yet — sleep until the next refill
            wait_needed = (1.0 - self._tokens) / self.refill_rate
            wait = min(wait_needed, deadline - time.monotonic(), 60.0)
            await asyncio.sleep(max(0.01, wait))

        logger.error("[rate_limit] %s: timed out waiting for token", self.method.value)
        return False


# ---------------------------------------------------------------------------
# RateLimiter — per-method bucket registry
# ---------------------------------------------------------------------------

class RateLimiter:
    """
    Registry of per-method token buckets.

    Usage:
        limiter = RateLimiter()
        await limiter.acquire(SlackMethod.CONVERSATIONS_HISTORY)
        # ... call Slack API ...
        # On 429:
        limiter.handle_429(SlackMethod.CONVERSATIONS_HISTORY, retry_after=60)
    """

    def __init__(self) -> None:
        self._buckets: dict[SlackMethod, _TokenBucket] = {
            method: _TokenBucket(method=method, rpm=rpm)
            for method, rpm in _METHOD_RPM.items()
        }
        self._rate_limit_hits: dict[SlackMethod, int] = {m: 0 for m in SlackMethod}
        self._app_rate_limited_count: int = 0
        self._total_retry_after_seconds: float = 0.0

    def handle_429(
        self,
        method: SlackMethod,
        retry_after: float = 60.0,
    ) -> float:
        """
        Record a 429 response and apply the Retry-After backoff to the method's bucket.

        Should be called whenever the Slack HTTP client receives a 429.
        Returns the retry_after seconds applied.
        """
        self._rate_limit_hits[method] = self._rate_limit_hits.get(method, 0) + 1
        bucket = self._buckets.get(method)
        if bucket:
            bucket.apply_retry_after(retry_after)
        self._total_retry_after_seconds += retry_after
        return retry_after

    def handle_app_rate_limited(self, minute_rate_limited: int) -> None:
        """
        Called when Slack delivers an app_rate_limited event.

        Indicates that event delivery itself was throttled — slow down proactive work.
        """
        self._app_rate_limited_count += 1
        logger.warning(
            "[rate_limit] app_rate_limited event received (minute=%d). "
            "Slack is throttling event delivery. Reducing proactive activity.",
            minute_rate_limited,
        )

    async def acquire(self, method: SlackMethod, timeout: float = 120.0) -> bool:
        """Acquire a rate-limit token for the given method (async, waits if needed)."""
        bucket = self._buckets.get(method)
        if bucket is None:
            # Unknown method — allow without limiting
            logger.debug("[rate_limit] No bucket for %s — allowing", method)
            return True
        return await bucket.acquire(timeout=timeout)

    def try_acquire(self, method: SlackMethod) -> bool:
        """Non-blocking acquire. Returns False immediately if no token available."""
        bucket = self._buckets.get(method)
        return bucket.try_acquire() if bucket else True

    def blocked_seconds(self, method: SlackMethod) -> float:
        """Seconds until this method is unblocked (0 = not blocked)."""
        bucket = self._buckets.get(method)
        return bucket.blocked_seconds() if bucket else 0.0

    def metrics(self) -> dict:
        """Return a snapshot of rate-limit hit counts for observability."""
        return {
            "rate_limit_hits": {m.value: n for m, n in self._rate_limit_hits.items() if n > 0},
            "app_rate_limited_events": self._app_rate_limited_count,
            "blocked_methods": {
                m.value: round(b.blocked_seconds(), 1)
                for m, b in self._buckets.items()
                if b.blocked_seconds() > 0
            },
            "total_retry_after_seconds": self._total_retry_after_seconds,
        }


# ---------------------------------------------------------------------------
# Retry helper for sync callers (non-async contexts)
# ---------------------------------------------------------------------------

def parse_retry_after(headers: dict) -> float:
    """
    Parse the Retry-After header from a Slack 429 response.
    Returns a float number of seconds (default 60 if header absent or unparseable).
    """
    val = headers.get("Retry-After") or headers.get("retry-after", "")
    try:
        return max(1.0, float(val))
    except (TypeError, ValueError):
        return 60.0  # Conservative default


class SlackApiError(Exception):
    """Raised by rate-limit-aware call wrappers when a Slack API call fails."""
    def __init__(self, method: str, error: str, status_code: int = 0):
        self.method = method
        self.error = error
        self.status_code = status_code
        super().__init__(f"Slack API error [{method}]: {error} (HTTP {status_code})")
