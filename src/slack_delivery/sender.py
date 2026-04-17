"""
Slack digest sender.

Sends a DailyDigest as a DM to a Slack user via the Slack Web API.
Supports:
  - chat.postMessage (primary path)
  - Incoming webhook (SLACK_WEBHOOK_URL env var — alternative path)
  - Dry-run mode (prints Block Kit payload, no API call)
  - Rate-limit retry with Retry-After compliance

Requires slack-sdk >= 3.26 for Web API path.
No dependencies for dry-run or webhook paths.
"""

from __future__ import annotations

import json
import logging
import os
import time

import urllib.request
import urllib.error

from src.models.derived import DailyDigest
from .block_kit import build_digest_blocks
from .config import SlackDeliveryConfig
from .exceptions import SlackDeliveryError

logger = logging.getLogger(__name__)


def send_digest(
    digest: DailyDigest,
    config: SlackDeliveryConfig,
    metrics=None,  # Optional[SlackDeliveryMetrics]
) -> bool:
    """
    Send a DailyDigest as a Slack DM.

    Args:
        digest: The digest to deliver.
        config: Delivery configuration (token, user map, dry_run flag).
        metrics: Optional SlackDeliveryMetrics instance to record counters.

    Returns:
        True if delivery succeeded (or dry_run), False on failure.
    """
    # Resolve Slack user ID
    slack_user_id = config.user_id_map.get(digest.user_id)
    if not slack_user_id:
        logger.warning(
            "No Slack user ID mapped for engine user '%s'. "
            "Add it to SLACK_USER_MAP. Skipping.",
            digest.user_id,
        )
        if metrics:
            metrics.users_skipped_no_mapping += 1
        return False

    blocks = build_digest_blocks(digest)
    fallback_text = digest.headline

    if config.dry_run:
        payload = {
            "channel": slack_user_id,
            "text": fallback_text,
            "blocks": blocks,
        }
        print(f"\n[DRY RUN] Would send to Slack user {slack_user_id} ({digest.user_id}):")
        print(json.dumps(payload, indent=2, default=str))
        if metrics:
            metrics.dry_run_generations += 1
        return True

    # Prefer incoming webhook if configured — avoids needing chat:write scope and slack-sdk
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if webhook_url:
        result = _send_via_webhook(webhook_url, blocks, fallback_text, digest.user_id)
        if metrics:
            metrics.webhook_deliveries += 1
            metrics.delivery_attempts += 1
            if result:
                metrics.delivery_successes += 1
            else:
                metrics.delivery_failures += 1
        return result

    # Web API path — requires slack-sdk
    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
    except ImportError:
        raise SlackDeliveryError(
            "slack-sdk is not installed. Run: pip install slack-sdk"
        )

    client = WebClient(token=config.bot_token)
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            response = client.chat_postMessage(
                channel=slack_user_id,
                text=fallback_text,
                blocks=blocks,
            )
            if response["ok"]:
                logger.info("Delivered digest to %s (%s)", digest.user_id, slack_user_id)
                if metrics:
                    metrics.delivery_successes += 1
                    metrics.delivery_attempts += 1
                return True
            else:
                logger.error("Slack API returned ok=false for %s: %s", digest.user_id, response)
                if metrics:
                    metrics.delivery_failures += 1
                return False

        except SlackApiError as e:
            error_code = e.response.get("error", "unknown")
            status = e.response.status_code if hasattr(e.response, "status_code") else 0

            if status == 429:
                retry_after = float(e.response.headers.get("Retry-After", 60))
                logger.warning(
                    "Rate-limited delivering to %s — Retry-After %.0fs (attempt %d/%d)",
                    digest.user_id, retry_after, attempt, max_retries,
                )
                if attempt < max_retries:
                    time.sleep(min(retry_after, 120))  # Cap at 2 min
                    continue

            logger.error(
                "Slack API error for %s (attempt %d/%d): %s",
                digest.user_id, attempt, max_retries, error_code,
            )
            if metrics:
                metrics.delivery_failures += 1
            return False

    if metrics:
        metrics.delivery_failures += 1
    return False


def _send_via_webhook(
    webhook_url: str,
    blocks: list[dict],
    fallback_text: str,
    user_id: str,
) -> bool:
    """
    Send a digest payload via Slack Incoming Webhook.

    Incoming webhooks post to a specific channel/user configured when the
    webhook was created. They don't support per-user targeting at send time.
    Useful as a simpler alternative to chat.postMessage for single-channel delivery.

    Reference: https://api.slack.com/messaging/webhooks
    """
    payload = json.dumps({"text": fallback_text, "blocks": blocks}).encode()
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode()
            if body == "ok":
                logger.info("Delivered digest for %s via webhook", user_id)
                return True
            logger.error("Webhook delivery returned unexpected body: %s", body)
            return False
    except urllib.error.HTTPError as e:
        logger.error("Webhook HTTP error %s for %s: %s", e.code, user_id, e.read())
        return False
    except urllib.error.URLError as e:
        logger.error("Webhook URL error for %s: %s", user_id, e.reason)
        return False
