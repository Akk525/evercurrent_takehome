"""
Slack integration metrics.

Lightweight counters for the Slack ingestion and delivery layers.
Designed to be threaded through the integration stack as an optional argument.

All values are simple integers — no external telemetry required.
Exposed via the /api/slack/metrics endpoint (if mounted).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class SlackIngestMetrics:
    """Counters for the Slack ingestion layer."""

    # Events received from Slack (via Events API or Socket Mode)
    events_received: int = 0
    # Events dropped because they were already processed (dedup by event_id)
    events_deduplicated: int = 0
    # Individual messages stored in SlackIngestStore
    messages_ingested: int = 0
    # Threads marked dirty by event processing (replies may be missing)
    dirty_threads_marked: int = 0

    # Reconciler: how many targeted conversations.replies / conversations.history calls were made
    reconciliation_reads: int = 0
    # Reconciler: how many threads were successfully reconciled (mark_thread_clean)
    reconciliation_successes: int = 0

    # Rate limiting
    rate_limit_hits: int = 0          # 429 responses received
    retry_after_total_seconds: float = 0.0  # Sum of Retry-After delays encountered

    # Slack-side event delivery rate limiting (app_rate_limited events)
    app_rate_limited_events: int = 0

    # Timestamps
    started_at: str = field(
        default_factory=lambda: datetime.now(tz=timezone.utc).isoformat()
    )
    last_event_at: str = ""

    def record_event(self) -> None:
        self.events_received += 1
        self.last_event_at = datetime.now(tz=timezone.utc).isoformat()

    def to_dict(self) -> dict:
        return {
            "events_received": self.events_received,
            "events_deduplicated": self.events_deduplicated,
            "messages_ingested": self.messages_ingested,
            "dirty_threads_marked": self.dirty_threads_marked,
            "reconciliation_reads": self.reconciliation_reads,
            "reconciliation_successes": self.reconciliation_successes,
            "rate_limit_hits": self.rate_limit_hits,
            "retry_after_total_seconds": self.retry_after_total_seconds,
            "app_rate_limited_events": self.app_rate_limited_events,
            "started_at": self.started_at,
            "last_event_at": self.last_event_at,
        }


@dataclass
class SlackDeliveryMetrics:
    """Counters for the Slack delivery layer."""

    # Delivery attempts per digest user
    delivery_attempts: int = 0
    # Successful deliveries (ok=True from Slack or dry_run=True)
    delivery_successes: int = 0
    # Failed deliveries (Slack API error, no token, no user mapping)
    delivery_failures: int = 0
    # Dry-run payload generations (printed, not sent)
    dry_run_generations: int = 0
    # Incoming webhook deliveries (alternative path)
    webhook_deliveries: int = 0
    # Users skipped because they have no Slack ID mapping
    users_skipped_no_mapping: int = 0

    # Rate limiting during delivery
    rate_limit_hits: int = 0

    def to_dict(self) -> dict:
        return {
            "delivery_attempts": self.delivery_attempts,
            "delivery_successes": self.delivery_successes,
            "delivery_failures": self.delivery_failures,
            "dry_run_generations": self.dry_run_generations,
            "webhook_deliveries": self.webhook_deliveries,
            "users_skipped_no_mapping": self.users_skipped_no_mapping,
            "rate_limit_hits": self.rate_limit_hits,
        }
