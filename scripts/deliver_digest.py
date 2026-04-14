#!/usr/bin/env python3
"""
deliver_digest.py — Deliver daily digests via Slack DM.

Runs the full pipeline, then sends each user's digest to Slack.
If SLACK_BOT_TOKEN is not set, falls back to dry-run mode (prints payloads).

Usage:
    python scripts/deliver_digest.py
    python scripts/deliver_digest.py --user u_alice
    python scripts/deliver_digest.py --dry-run
    python scripts/deliver_digest.py --date 2026-04-10
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data" / "mock_slack"
NOW = datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc)


def main() -> None:
    parser = argparse.ArgumentParser(description="Deliver daily digests via Slack DM")
    parser.add_argument("--user", help="Deliver only for this engine user ID (e.g. u_alice)")
    parser.add_argument("--dry-run", action="store_true", help="Print payloads instead of sending")
    parser.add_argument("--date", default="2026-04-10", help="Digest date (default: 2026-04-10)")
    args = parser.parse_args()

    # Run the pipeline
    from src.digest.assembler import run_full_pipeline

    logger.info("Running digest pipeline...")
    user_ids = [args.user] if args.user else None
    digests = run_full_pipeline(
        data_dir=DATA_DIR,
        user_ids=user_ids,
        now=NOW,
        date_str=args.date,
    )
    logger.info("Generated %d digest(s)", len(digests))

    # Load Slack config
    from src.slack_delivery.config import load_config

    config = load_config()

    if config is None:
        logger.warning(
            "SLACK_BOT_TOKEN not set — switching to dry-run mode. "
            "Set SLACK_BOT_TOKEN to send real Slack messages."
        )
        # Build a minimal dry-run config
        from src.slack_delivery.config import SlackDeliveryConfig
        config = SlackDeliveryConfig(bot_token="", user_id_map={}, dry_run=True)

    if args.dry_run:
        config.dry_run = True

    # Send digests
    from src.slack_delivery.sender import send_digest

    results: dict[str, bool] = {}
    for user_id, digest in digests.items():
        logger.info("Delivering digest for %s...", user_id)
        success = send_digest(digest, config)
        results[user_id] = success

    # Summary
    print("\n--- Delivery summary ---")
    for uid, ok in results.items():
        status = "sent" if ok else "failed/skipped"
        print(f"  {uid}: {status}")

    failed = [uid for uid, ok in results.items() if not ok]
    if failed:
        print(f"\n{len(failed)} user(s) not delivered. Check SLACK_USER_MAP mapping.")
        sys.exit(1)
    else:
        print(f"\nAll {len(results)} digest(s) delivered successfully.")


if __name__ == "__main__":
    main()
