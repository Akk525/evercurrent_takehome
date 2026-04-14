"""
Slack digest sender.

Sends a DailyDigest as a DM to a Slack user via the Slack Web API.
Requires slack-sdk >= 3.26.
"""

from __future__ import annotations

import json
import logging

from src.models.derived import DailyDigest
from .block_kit import build_digest_blocks
from .config import SlackDeliveryConfig
from .exceptions import SlackDeliveryError

logger = logging.getLogger(__name__)


def send_digest(digest: DailyDigest, config: SlackDeliveryConfig) -> bool:
    """
    Send a DailyDigest as a Slack DM.

    Args:
        digest: The digest to deliver.
        config: Delivery configuration (token, user map, dry_run flag).

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
        return True

    # Real send path
    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
    except ImportError:
        raise SlackDeliveryError(
            "slack-sdk is not installed. Run: pip install slack-sdk"
        )

    client = WebClient(token=config.bot_token)
    try:
        response = client.chat_postMessage(
            channel=slack_user_id,
            text=fallback_text,
            blocks=blocks,
        )
        if response["ok"]:
            logger.info("Delivered digest to %s (%s)", digest.user_id, slack_user_id)
            return True
        else:
            logger.error("Slack API returned ok=false for %s: %s", digest.user_id, response)
            return False
    except SlackApiError as e:
        logger.error("Slack API error for %s: %s", digest.user_id, e.response["error"])
        return False
