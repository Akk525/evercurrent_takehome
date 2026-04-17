"""
HTTP Events API handler.

Handles incoming Slack Events API HTTP POST requests:
1. URL verification challenge (Slack handshake)
2. Event delivery (message events, etc.)
3. Request signature verification (HMAC-SHA256)

This module is imported by api/slack_events.py to mount as a FastAPI router.

Slack signing secret verification reference:
  https://api.slack.com/authentication/verifying-requests-from-slack
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)


def verify_slack_signature(
    body: bytes,
    timestamp: str,
    signature: str,
    signing_secret: str,
) -> bool:
    """
    Verify a Slack request signature using HMAC-SHA256.

    Slack sends:
        X-Slack-Request-Timestamp: <unix timestamp>
        X-Slack-Signature: v0=<hex HMAC>

    We compute:
        basestring = "v0:" + timestamp + ":" + raw_body
        expected = "v0=" + HMAC-SHA256(signing_secret, basestring).hexdigest()

    Returns True if the computed signature matches and the request is fresh (< 5 min old).
    """
    # Replay protection: reject requests older than 5 minutes
    try:
        ts = int(timestamp)
        if abs(time.time() - ts) > 300:
            logger.warning("[signature] Request timestamp too old: %s", timestamp)
            return False
    except (TypeError, ValueError):
        logger.warning("[signature] Invalid timestamp: %s", timestamp)
        return False

    basestring = f"v0:{timestamp}:{body.decode('utf-8', errors='replace')}".encode()
    computed = "v0=" + hmac.new(
        signing_secret.encode(),
        basestring,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(computed, signature):
        logger.warning("[signature] Signature mismatch — possible forged request")
        return False

    return True


def get_signing_secret() -> Optional[str]:
    """Return SLACK_SIGNING_SECRET from env, or None if not configured."""
    return os.environ.get("SLACK_SIGNING_SECRET", "").strip() or None
