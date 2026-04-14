"""
Block Kit payload builder for DailyDigest.

Generates Slack Block Kit blocks from a DailyDigest object.
Resulting list is JSON-serialisable and suitable for chat.postMessage.
"""

from __future__ import annotations

from src.models.derived import DailyDigest, RankedDigestItem

_SIGNAL_EMOJI = {
    "high": "🔴",
    "medium": "🟡",
    "low": "🟢",
}

_EVENT_TYPE_EMOJI = {
    "blocker": "🚫",
    "risk": "⚠️",
    "decision": "🗳️",
    "status_update": "📊",
    "request_for_input": "💬",
    "noise": "💭",
}


def build_digest_blocks(digest: DailyDigest) -> list[dict]:
    """
    Build a Slack Block Kit payload for a DailyDigest.

    Structure:
    1. Header — digest title + date
    2. Headline text
    3. Divider
    4. For each ranked item:
       - Title with signal emoji
       - Event type + confidence context
       - Summary (if present)
       - Why shown (if present)
       - Source thread IDs
       - Divider
    5. Footer — metadata
    """
    blocks: list[dict] = []

    # 1. Header
    blocks.append({
        "type": "header",
        "text": {
            "type": "plain_text",
            "text": f"📋 Daily Digest — {digest.date}",
            "emoji": True,
        },
    })

    # 2. Headline
    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f"*{digest.headline}*",
        },
    })

    # 3. Divider
    blocks.append({"type": "divider"})

    # 4. Ranked items
    for i, item in enumerate(digest.items, start=1):
        blocks.extend(_item_blocks(item, rank=i))

    # 5. Footer
    llm_note = "AI-summarised" if digest.llm_used else "Rule-based summary"
    blocks.append({
        "type": "context",
        "elements": [
            {
                "type": "mrkdwn",
                "text": (
                    f"Generated {digest.generated_at.strftime('%Y-%m-%d %H:%M UTC') if hasattr(digest.generated_at, 'strftime') else digest.generated_at} "
                    f"• {digest.total_candidates_considered} candidates considered "
                    f"• {llm_note}"
                ),
            }
        ],
    })

    return blocks


def _item_blocks(item: RankedDigestItem, rank: int) -> list[dict]:
    """Build Block Kit blocks for a single ranked digest item."""
    blocks: list[dict] = []

    signal_emoji = _SIGNAL_EMOJI.get(item.signal_level, "⚪")
    event_emoji = _EVENT_TYPE_EMOJI.get(item.event_type, "📌")

    # Title block
    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f"{signal_emoji} *{rank}. {item.title}*",
        },
    })

    # Event type + confidence context
    event_type_label = item.event_type.replace("_", " ").title()
    blocks.append({
        "type": "context",
        "elements": [
            {
                "type": "mrkdwn",
                "text": (
                    f"{event_emoji} {event_type_label} "
                    f"• Confidence: {item.confidence * 100:.0f}% "
                    f"• Score: {item.score:.3f}"
                ),
            }
        ],
    })

    # Summary
    if item.summary:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": item.summary,
            },
        })

    # Why shown
    if item.why_shown:
        blocks.append({
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"_💡 {item.why_shown}_",
                }
            ],
        })

    # Source thread IDs
    if item.source_thread_ids:
        thread_list = ", ".join(f"`{tid}`" for tid in item.source_thread_ids)
        blocks.append({
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"Source thread(s): {thread_list}",
                }
            ],
        })

    blocks.append({"type": "divider"})
    return blocks
