"""
Slack delivery configuration.

Loads credentials from environment variables.
Returns None gracefully when not configured — never raises.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class SlackDeliveryConfig:
    """Configuration for Slack delivery."""
    bot_token: str
    # Maps engine user_id (e.g. "u_alice") to real Slack member ID (e.g. "U012AB3CD")
    user_id_map: dict[str, str] = field(default_factory=dict)
    # If True: print Block Kit payload instead of sending
    dry_run: bool = False


def load_config() -> SlackDeliveryConfig | None:
    """
    Load Slack delivery config from environment variables.

    Required env vars:
      SLACK_BOT_TOKEN   — bot OAuth token (xoxb-...)

    Optional env vars:
      SLACK_USER_MAP    — JSON string mapping engine IDs to Slack IDs
                          e.g. '{"u_alice": "U012AB3CD", "u_bob": "U034EF5GH"}'
      SLACK_DRY_RUN     — if set to "1" or "true", enables dry-run mode

    Returns None if SLACK_BOT_TOKEN is not set (never raises).
    """
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if not token:
        logger.info("SLACK_BOT_TOKEN not set — Slack delivery disabled")
        return None

    user_map: dict[str, str] = {}
    raw_map = os.environ.get("SLACK_USER_MAP", "").strip()
    if raw_map:
        try:
            user_map = json.loads(raw_map)
        except json.JSONDecodeError:
            logger.warning("SLACK_USER_MAP is not valid JSON — user map will be empty")

    dry_run_val = os.environ.get("SLACK_DRY_RUN", "").strip().lower()
    dry_run = dry_run_val in ("1", "true", "yes")

    return SlackDeliveryConfig(
        bot_token=token,
        user_id_map=user_map,
        dry_run=dry_run,
    )
