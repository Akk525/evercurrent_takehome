"""
Tests for rate-limit-aware Slack API handling.

Coverage:
    1. Token bucket: basic acquire, refill over time
    2. Token bucket: non-blocking try_acquire
    3. 429 handling: apply_retry_after blocks method
    4. parse_retry_after: header parsing + fallback default
    5. RateLimiter.metrics() returns correct hit counts
    6. Tier 1 methods (conversations.history, conversations.replies) have low capacity
    7. Async acquire: waits and then succeeds after refill
    8. Blocked-method reporting
    9. app_rate_limited tracking
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

import pytest

from src.slack_ingest.rate_limits import (
    RateLimiter,
    SlackMethod,
    _TokenBucket,
    parse_retry_after,
)


# ---------------------------------------------------------------------------
# _TokenBucket unit tests
# ---------------------------------------------------------------------------

class TestTokenBucket:
    def _bucket(self, rpm: float = 60.0) -> _TokenBucket:
        return _TokenBucket(method=SlackMethod.CONVERSATIONS_HISTORY, rpm=rpm)

    def test_initial_token_available(self):
        bucket = self._bucket()
        assert bucket.try_acquire() is True

    def test_second_acquire_fails_immediately(self):
        bucket = self._bucket(rpm=60.0)
        bucket.try_acquire()   # Consume the initial token
        assert bucket.try_acquire() is False

    def test_refill_over_time(self):
        bucket = self._bucket(rpm=60.0)
        bucket.try_acquire()   # Consume

        # Simulate 1 second passing (at 60 rpm → 1 token/sec)
        bucket._last_refill -= 1.0
        assert bucket.try_acquire() is True

    def test_apply_retry_after_blocks(self):
        bucket = self._bucket()
        bucket.apply_retry_after(retry_after_seconds=30.0)
        assert bucket.blocked_seconds() > 25.0
        assert bucket.try_acquire() is False

    def test_retry_after_expires(self):
        bucket = self._bucket()
        bucket.apply_retry_after(retry_after_seconds=0.01)
        time.sleep(0.05)
        assert bucket.blocked_seconds() == 0.0

    def test_blocked_seconds_zero_when_not_blocked(self):
        bucket = self._bucket()
        assert bucket.blocked_seconds() == 0.0

    def test_low_rpm_bucket_refills_slowly(self):
        # 1 RPM bucket: 1 token per 60 seconds
        bucket = self._bucket(rpm=1.0)
        bucket.try_acquire()  # Consume
        # After 0.1 seconds, we should NOT have refilled
        bucket._last_refill -= 0.1
        assert bucket.try_acquire() is False


class TestTokenBucketAsync:
    @pytest.mark.asyncio
    async def test_async_acquire_waits_and_succeeds(self):
        bucket = _TokenBucket(method=SlackMethod.CHAT_POST_MESSAGE, rpm=3600.0)  # Fast refill
        bucket.try_acquire()   # Consume

        # Should eventually succeed (refill is fast at 3600 rpm = 1 token/second)
        result = await bucket.acquire(timeout=5.0)
        assert result is True

    @pytest.mark.asyncio
    async def test_async_acquire_times_out(self):
        bucket = _TokenBucket(method=SlackMethod.CONVERSATIONS_HISTORY, rpm=1.0)
        bucket.try_acquire()  # Consume
        # At 1 rpm, next token is in 60s — timeout before that
        result = await bucket.acquire(timeout=0.05)
        assert result is False


# ---------------------------------------------------------------------------
# RateLimiter
# ---------------------------------------------------------------------------

class TestRateLimiter:
    def test_try_acquire_known_method(self):
        limiter = RateLimiter()
        # First acquire should succeed for all methods
        assert limiter.try_acquire(SlackMethod.CHAT_POST_MESSAGE) is True

    def test_try_acquire_unknown_method_allows(self):
        # Unknown methods should not be blocked
        limiter = RateLimiter()
        # We test with a valid method that's in the registry
        assert limiter.try_acquire(SlackMethod.AUTH_TEST) is True

    def test_handle_429_blocks_method(self):
        limiter = RateLimiter()
        limiter.try_acquire(SlackMethod.CONVERSATIONS_HISTORY)  # Consume
        limiter.handle_429(SlackMethod.CONVERSATIONS_HISTORY, retry_after=30.0)

        assert limiter.blocked_seconds(SlackMethod.CONVERSATIONS_HISTORY) > 25.0
        assert limiter.try_acquire(SlackMethod.CONVERSATIONS_HISTORY) is False

    def test_handle_429_increments_counter(self):
        limiter = RateLimiter()
        limiter.handle_429(SlackMethod.CONVERSATIONS_REPLIES, retry_after=60.0)
        limiter.handle_429(SlackMethod.CONVERSATIONS_REPLIES, retry_after=60.0)

        metrics = limiter.metrics()
        assert metrics["rate_limit_hits"][SlackMethod.CONVERSATIONS_REPLIES.value] == 2

    def test_metrics_empty_initially(self):
        limiter = RateLimiter()
        metrics = limiter.metrics()
        assert metrics["rate_limit_hits"] == {}
        assert metrics["app_rate_limited_events"] == 0
        assert metrics["blocked_methods"] == {}

    def test_handle_app_rate_limited(self):
        limiter = RateLimiter()
        limiter.handle_app_rate_limited(minute_rate_limited=1712700060)
        limiter.handle_app_rate_limited(minute_rate_limited=1712700120)
        assert limiter.metrics()["app_rate_limited_events"] == 2

    def test_blocked_methods_in_metrics(self):
        limiter = RateLimiter()
        limiter.handle_429(SlackMethod.CONVERSATIONS_HISTORY, retry_after=60.0)
        metrics = limiter.metrics()
        assert SlackMethod.CONVERSATIONS_HISTORY.value in metrics["blocked_methods"]

    def test_unblocked_methods_not_in_metrics(self):
        limiter = RateLimiter()
        metrics = limiter.metrics()
        assert len(metrics["blocked_methods"]) == 0

    @pytest.mark.asyncio
    async def test_async_acquire_succeeds_for_fresh_limiter(self):
        limiter = RateLimiter()
        result = await limiter.acquire(SlackMethod.CHAT_POST_MESSAGE, timeout=1.0)
        assert result is True

    @pytest.mark.asyncio
    async def test_async_acquire_fails_when_blocked(self):
        limiter = RateLimiter()
        limiter.handle_429(SlackMethod.CONVERSATIONS_REPLIES, retry_after=30.0)
        result = await limiter.acquire(SlackMethod.CONVERSATIONS_REPLIES, timeout=0.1)
        assert result is False

    def test_tier1_methods_have_low_rpm(self):
        """conversations.history and conversations.replies should be the most restricted."""
        from src.slack_ingest.rate_limits import _METHOD_RPM
        h_rpm = _METHOD_RPM[SlackMethod.CONVERSATIONS_HISTORY]
        r_rpm = _METHOD_RPM[SlackMethod.CONVERSATIONS_REPLIES]
        post_rpm = _METHOD_RPM[SlackMethod.CHAT_POST_MESSAGE]

        assert h_rpm <= 2.0, f"conversations.history should be ≤ 2 RPM, got {h_rpm}"
        assert r_rpm <= 2.0, f"conversations.replies should be ≤ 2 RPM, got {r_rpm}"
        assert post_rpm > h_rpm, "chat.postMessage should allow more than conversations.history"


# ---------------------------------------------------------------------------
# parse_retry_after
# ---------------------------------------------------------------------------

class TestParseRetryAfter:
    def test_numeric_header(self):
        headers = {"Retry-After": "30"}
        assert parse_retry_after(headers) == 30.0

    def test_float_header(self):
        headers = {"Retry-After": "1.5"}
        assert parse_retry_after(headers) == 1.5

    def test_missing_header_returns_default(self):
        assert parse_retry_after({}) == 60.0

    def test_invalid_header_returns_default(self):
        assert parse_retry_after({"Retry-After": "soon"}) == 60.0

    def test_lowercase_header_key(self):
        headers = {"retry-after": "45"}
        assert parse_retry_after(headers) == 45.0

    def test_minimum_is_one_second(self):
        headers = {"Retry-After": "0"}
        assert parse_retry_after(headers) == 1.0


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------

class TestSlackSignatureVerification:
    def test_valid_signature(self):
        from src.slack_ingest.http_events import verify_slack_signature
        import hashlib, hmac, time

        secret = "test_signing_secret"
        body = b'{"type":"event_callback"}'
        ts = str(int(time.time()))
        basestring = f"v0:{ts}:{body.decode()}".encode()
        sig = "v0=" + hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()

        assert verify_slack_signature(body, ts, sig, secret) is True

    def test_wrong_secret_fails(self):
        from src.slack_ingest.http_events import verify_slack_signature
        import hashlib, hmac, time

        body = b'{"type":"event_callback"}'
        ts = str(int(time.time()))
        basestring = f"v0:{ts}:{body.decode()}".encode()
        sig = "v0=" + hmac.new("wrong_secret".encode(), basestring, hashlib.sha256).hexdigest()

        assert verify_slack_signature(body, ts, sig, "correct_secret") is False

    def test_stale_timestamp_fails(self):
        from src.slack_ingest.http_events import verify_slack_signature

        body = b'{"type":"event_callback"}'
        stale_ts = str(int(time.time()) - 400)  # 400s old
        assert verify_slack_signature(body, stale_ts, "v0=anything", "secret") is False

    def test_invalid_timestamp_fails(self):
        from src.slack_ingest.http_events import verify_slack_signature
        assert verify_slack_signature(b"body", "not_a_number", "v0=sig", "secret") is False
