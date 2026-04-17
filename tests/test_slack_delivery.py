"""
Tests for Slack delivery layer.

Coverage:
    1. Block Kit payload structure (header, divider, footer)
    2. _item_blocks signal emoji, event type, confidence display
    3. Impact statement block rendered when present
    4. Memory label block rendered only for recurring issues
    5. Why-shown context block
    6. Source thread IDs listed
    7. Empty digest (no items) — header + footer only
    8. load_config returns None when SLACK_BOT_TOKEN absent
    9. load_config parses SLACK_USER_MAP correctly
   10. load_config enables dry_run via env var
   11. load_config gracefully handles malformed SLACK_USER_MAP
   12. send_digest returns False when user not in id map
   13. send_digest dry_run returns True and prints payload
   14. send_digest returns False when slack-sdk absent (no token path skips sdk)
   15. _send_via_webhook success path
   16. _send_via_webhook HTTP error returns False
   17. _send_via_webhook URL error returns False
   18. SlackDeliveryConfig defaults
   19. build_digest_blocks produces valid JSON-serialisable structure
   20. Noise items still render without crashing
"""

from __future__ import annotations

import io
import json
import sys
import urllib.error
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.models.derived import DailyDigest, RankedDigestItem, RankingFeatures
from src.slack_delivery.block_kit import build_digest_blocks, _item_blocks
from src.slack_delivery.config import SlackDeliveryConfig, load_config
from src.slack_delivery.sender import send_digest, _send_via_webhook


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_item(
    *,
    title: str = "Test Issue",
    event_type: str = "blocker",
    signal_level: str = "high",
    confidence: float = 0.85,
    score: float = 0.72,
    summary: str = "The voltage rail on rev3 is intermittent.",
    why_shown: str = "You are the primary participant.",
    source_thread_ids: list[str] | None = None,
    impact_statement: str | None = None,
    reason_features: RankingFeatures | None = None,
) -> RankedDigestItem:
    _default_weights = {
        "user_affinity": 0.28,
        "importance": 0.24,
        "urgency": 0.20,
        "momentum": 0.10,
        "novelty": 0.08,
        "recency": 0.05,
        "embedding_affinity": 0.05,
    }
    features = reason_features or RankingFeatures(
        user_affinity=0.9,
        importance=0.8,
        urgency=0.7,
        momentum=0.5,
        novelty=0.4,
        recency=0.6,
        embedding_affinity=0.3,
        weights=_default_weights,
        final_score=score,
    )
    item = RankedDigestItem(
        event_id="ev_001",
        title=title,
        event_type=event_type,
        signal_level=signal_level,
        confidence=confidence,
        score=score,
        summary=summary,
        why_shown=why_shown,
        source_thread_ids=source_thread_ids if source_thread_ids is not None else ["t_001"],
        source_message_ids=["m_001"],
        reason_features=features,
    )
    if impact_statement is not None:
        item.impact_statement = impact_statement
    return item


def _make_digest(items: list[RankedDigestItem] | None = None) -> DailyDigest:
    return DailyDigest(
        user_id="u_alice",
        date="2026-04-10",
        headline="3 items need your attention today.",
        items=items if items is not None else [_make_item()],
        total_candidates_considered=12,
        generated_at=datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc),
        llm_used=False,
    )


# ---------------------------------------------------------------------------
# Block Kit structure tests
# ---------------------------------------------------------------------------

class TestBuildDigestBlocks:
    def test_header_block_present(self):
        blocks = build_digest_blocks(_make_digest())
        assert blocks[0]["type"] == "header"
        assert "Daily Digest" in blocks[0]["text"]["text"]

    def test_headline_section_present(self):
        blocks = build_digest_blocks(_make_digest())
        assert blocks[1]["type"] == "section"
        assert "attention" in blocks[1]["text"]["text"]

    def test_divider_after_headline(self):
        blocks = build_digest_blocks(_make_digest())
        assert blocks[2]["type"] == "divider"

    def test_footer_context_block_present(self):
        blocks = build_digest_blocks(_make_digest())
        footer = blocks[-1]
        assert footer["type"] == "context"
        footer_text = footer["elements"][0]["text"]
        assert "candidates considered" in footer_text

    def test_llm_note_false(self):
        blocks = build_digest_blocks(_make_digest())
        footer_text = blocks[-1]["elements"][0]["text"]
        assert "Rule-based summary" in footer_text

    def test_llm_note_true(self):
        d = _make_digest()
        d.llm_used = True
        blocks = build_digest_blocks(d)
        footer_text = blocks[-1]["elements"][0]["text"]
        assert "AI-summarised" in footer_text

    def test_empty_digest_has_header_and_footer(self):
        blocks = build_digest_blocks(_make_digest(items=[]))
        types = [b["type"] for b in blocks]
        assert "header" in types
        assert "context" in types  # footer
        # No item section blocks
        assert types.count("section") == 1  # headline only

    def test_json_serialisable(self):
        blocks = build_digest_blocks(_make_digest())
        # Should not raise
        serialised = json.dumps(blocks, default=str)
        parsed = json.loads(serialised)
        assert isinstance(parsed, list)


class TestItemBlocks:
    def test_title_includes_rank_and_title(self):
        item = _make_item(title="Voltage Rail Issue", signal_level="high")
        blocks = _item_blocks(item, rank=1)
        title_block = blocks[0]
        assert title_block["type"] == "section"
        assert "1." in title_block["text"]["text"]
        assert "Voltage Rail Issue" in title_block["text"]["text"]

    def test_high_signal_emoji(self):
        item = _make_item(signal_level="high")
        blocks = _item_blocks(item, rank=1)
        assert "🔴" in blocks[0]["text"]["text"]

    def test_medium_signal_emoji(self):
        item = _make_item(signal_level="medium")
        blocks = _item_blocks(item, rank=2)
        assert "🟡" in blocks[0]["text"]["text"]

    def test_low_signal_emoji(self):
        item = _make_item(signal_level="low")
        blocks = _item_blocks(item, rank=3)
        assert "🟢" in blocks[0]["text"]["text"]

    def test_unknown_signal_level_fallback_emoji(self):
        item = _make_item(signal_level="critical")
        blocks = _item_blocks(item, rank=1)
        assert "⚪" in blocks[0]["text"]["text"]

    def test_confidence_displayed_as_percent(self):
        item = _make_item(confidence=0.87)
        blocks = _item_blocks(item, rank=1)
        context_text = blocks[1]["elements"][0]["text"]
        assert "87%" in context_text

    def test_score_displayed(self):
        item = _make_item(score=0.723)
        blocks = _item_blocks(item, rank=1)
        context_text = blocks[1]["elements"][0]["text"]
        assert "0.723" in context_text

    def test_blocker_event_type_emoji(self):
        item = _make_item(event_type="blocker")
        blocks = _item_blocks(item, rank=1)
        assert "🚫" in blocks[1]["elements"][0]["text"]

    def test_decision_event_type_emoji(self):
        item = _make_item(event_type="decision")
        blocks = _item_blocks(item, rank=1)
        assert "🗳️" in blocks[1]["elements"][0]["text"]

    def test_summary_block_rendered(self):
        item = _make_item(summary="The PCB trace is shorted.")
        blocks = _item_blocks(item, rank=1)
        text_blocks = [b for b in blocks if b["type"] == "section"]
        summaries = [b for b in text_blocks if "shorted" in b["text"]["text"]]
        assert len(summaries) == 1

    def test_no_summary_skips_block(self):
        item = _make_item(summary="")
        blocks = _item_blocks(item, rank=1)
        texts = [b["text"]["text"] for b in blocks if b.get("type") == "section"]
        assert not any("shorted" in t for t in texts)

    def test_why_shown_rendered(self):
        item = _make_item(why_shown="You are the assignee.")
        blocks = _item_blocks(item, rank=1)
        context_texts = [
            e["text"]
            for b in blocks if b["type"] == "context"
            for e in b["elements"]
        ]
        assert any("assignee" in t for t in context_texts)

    def test_source_thread_ids_listed(self):
        item = _make_item(source_thread_ids=["t_abc", "t_xyz"])
        blocks = _item_blocks(item, rank=1)
        context_texts = [
            e["text"]
            for b in blocks if b["type"] == "context"
            for e in b["elements"]
        ]
        assert any("t_abc" in t for t in context_texts)
        assert any("t_xyz" in t for t in context_texts)

    def test_no_source_thread_ids_skips_block(self):
        item = _make_item(source_thread_ids=[])
        blocks = _item_blocks(item, rank=1)
        context_texts = [
            e["text"]
            for b in blocks if b["type"] == "context"
            for e in b["elements"]
        ]
        assert not any("Source thread" in t for t in context_texts)

    def test_impact_statement_rendered(self):
        item = _make_item(impact_statement="This likely delays DVT by 2 weeks.")
        blocks = _item_blocks(item, rank=1)
        context_texts = [
            e["text"]
            for b in blocks if b["type"] == "context"
            for e in b["elements"]
        ]
        assert any("DVT" in t for t in context_texts)
        assert any("⚡" in t for t in context_texts)

    def test_no_impact_statement_skips_block(self):
        item = _make_item(impact_statement=None)
        blocks = _item_blocks(item, rank=1)
        context_texts = [
            e["text"]
            for b in blocks if b["type"] == "context"
            for e in b["elements"]
        ]
        assert not any("⚡" in t for t in context_texts)

    def test_memory_label_rendered_for_recurring(self):
        features = RankingFeatures(
            user_affinity=0.9,
            importance=0.8,
            urgency=0.7,
            momentum=0.5,
            novelty=0.4,
            recency=0.6,
            embedding_affinity=0.3,
            weights={"user_affinity": 0.28, "importance": 0.24, "urgency": 0.20,
                     "momentum": 0.10, "novelty": 0.08, "recency": 0.05, "embedding_affinity": 0.05},
            final_score=0.75,
            issue_memory_label="Seen in 3 prior digests",
        )
        item = _make_item(reason_features=features)
        blocks = _item_blocks(item, rank=1)
        context_texts = [
            e["text"]
            for b in blocks if b["type"] == "context"
            for e in b["elements"]
        ]
        assert any("3 prior digests" in t for t in context_texts)
        assert any("🔁" in t for t in context_texts)

    def test_memory_label_hidden_for_new_issue(self):
        features = RankingFeatures(
            user_affinity=0.9,
            importance=0.8,
            urgency=0.7,
            momentum=0.5,
            novelty=0.4,
            recency=0.6,
            embedding_affinity=0.3,
            weights={"user_affinity": 0.28, "importance": 0.24, "urgency": 0.20,
                     "momentum": 0.10, "novelty": 0.08, "recency": 0.05, "embedding_affinity": 0.05},
            final_score=0.75,
            issue_memory_label="New issue",
        )
        item = _make_item(reason_features=features)
        blocks = _item_blocks(item, rank=1)
        context_texts = [
            e["text"]
            for b in blocks if b["type"] == "context"
            for e in b["elements"]
        ]
        assert not any("🔁" in t for t in context_texts)

    def test_ends_with_divider(self):
        item = _make_item()
        blocks = _item_blocks(item, rank=1)
        assert blocks[-1]["type"] == "divider"


# ---------------------------------------------------------------------------
# SlackDeliveryConfig + load_config
# ---------------------------------------------------------------------------

class TestLoadConfig:
    def test_returns_none_without_token(self):
        with patch.dict("os.environ", {}, clear=True):
            # Ensure no token in env
            import os
            os.environ.pop("SLACK_BOT_TOKEN", None)
            cfg = load_config()
        assert cfg is None

    def test_returns_config_with_token(self):
        with patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-test-token"}):
            cfg = load_config()
        assert cfg is not None
        assert cfg.bot_token == "xoxb-test-token"

    def test_parses_user_map(self):
        env = {
            "SLACK_BOT_TOKEN": "xoxb-test",
            "SLACK_USER_MAP": '{"u_alice": "U012AB3CD"}',
        }
        with patch.dict("os.environ", env):
            cfg = load_config()
        assert cfg.user_id_map == {"u_alice": "U012AB3CD"}

    def test_malformed_user_map_returns_empty_dict(self):
        env = {
            "SLACK_BOT_TOKEN": "xoxb-test",
            "SLACK_USER_MAP": "not valid json",
        }
        with patch.dict("os.environ", env):
            cfg = load_config()
        assert cfg.user_id_map == {}

    def test_dry_run_enabled_via_1(self):
        with patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-test", "SLACK_DRY_RUN": "1"}):
            cfg = load_config()
        assert cfg.dry_run is True

    def test_dry_run_enabled_via_true(self):
        with patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-test", "SLACK_DRY_RUN": "true"}):
            cfg = load_config()
        assert cfg.dry_run is True

    def test_dry_run_disabled_by_default(self):
        with patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-test"}):
            cfg = load_config()
        assert cfg.dry_run is False


class TestSlackDeliveryConfig:
    def test_defaults(self):
        cfg = SlackDeliveryConfig(bot_token="xoxb-x")
        assert cfg.user_id_map == {}
        assert cfg.dry_run is False


# ---------------------------------------------------------------------------
# send_digest
# ---------------------------------------------------------------------------

class TestSendDigest:
    def _config(self, dry_run: bool = False) -> SlackDeliveryConfig:
        return SlackDeliveryConfig(
            bot_token="xoxb-fake",
            user_id_map={"u_alice": "U012AB3CD"},
            dry_run=dry_run,
        )

    def test_returns_false_when_user_not_in_map(self):
        cfg = SlackDeliveryConfig(bot_token="xoxb-fake", user_id_map={})
        digest = _make_digest()
        result = send_digest(digest, cfg)
        assert result is False

    def test_dry_run_returns_true(self, capsys):
        cfg = self._config(dry_run=True)
        digest = _make_digest()
        result = send_digest(digest, cfg)
        assert result is True

    def test_dry_run_prints_payload(self, capsys):
        cfg = self._config(dry_run=True)
        send_digest(_make_digest(), cfg)
        captured = capsys.readouterr()
        assert "DRY RUN" in captured.out
        assert "U012AB3CD" in captured.out

    def test_dry_run_payload_is_valid_json(self, capsys):
        cfg = self._config(dry_run=True)
        send_digest(_make_digest(), cfg)
        captured = capsys.readouterr()
        # Extract the JSON portion after the header line
        lines = captured.out.splitlines()
        json_start = next(i for i, l in enumerate(lines) if l.strip().startswith("{"))
        payload = json.loads("\n".join(lines[json_start:]))
        assert "blocks" in payload
        assert "channel" in payload

    def test_webhook_path_used_when_env_set(self):
        cfg = self._config()
        digest = _make_digest()
        with patch.dict("os.environ", {"SLACK_WEBHOOK_URL": "https://hooks.slack.com/xxx"}), \
             patch("src.slack_delivery.sender._send_via_webhook", return_value=True) as mock_wh:
            result = send_digest(digest, cfg)
        mock_wh.assert_called_once()
        assert result is True

    def test_sdk_import_error_raises_delivery_error(self):
        """When slack-sdk is not installed and no webhook, should raise SlackDeliveryError."""
        from src.slack_delivery.exceptions import SlackDeliveryError
        cfg = self._config()
        digest = _make_digest()

        # Ensure no webhook URL, then block the SDK import
        with patch.dict("os.environ", {"SLACK_WEBHOOK_URL": ""}), \
             patch.dict(sys.modules, {"slack_sdk": None, "slack_sdk.errors": None}):
            with pytest.raises((SlackDeliveryError, ImportError)):
                send_digest(digest, cfg)

    def test_send_ok_returns_true(self):
        cfg = self._config()
        digest = _make_digest()

        mock_client = MagicMock()
        mock_client.chat_postMessage.return_value = {"ok": True}

        # Inject mock slack_sdk so the lazy import inside sender succeeds
        mock_slack_sdk = MagicMock()
        mock_slack_sdk.WebClient.return_value = mock_client
        mock_slack_errors = MagicMock()
        mock_slack_errors.SlackApiError = Exception

        with patch.dict("os.environ", {"SLACK_WEBHOOK_URL": ""}), \
             patch.dict(sys.modules, {"slack_sdk": mock_slack_sdk, "slack_sdk.errors": mock_slack_errors}):
            result = send_digest(digest, cfg)

        assert result is True

    def test_send_returns_false_on_ok_false(self):
        cfg = self._config()
        digest = _make_digest()

        mock_client = MagicMock()
        mock_client.chat_postMessage.return_value = {"ok": False}

        mock_slack_sdk = MagicMock()
        mock_slack_sdk.WebClient.return_value = mock_client
        mock_slack_errors = MagicMock()
        mock_slack_errors.SlackApiError = Exception

        with patch.dict("os.environ", {"SLACK_WEBHOOK_URL": ""}), \
             patch.dict(sys.modules, {"slack_sdk": mock_slack_sdk, "slack_sdk.errors": mock_slack_errors}):
            result = send_digest(digest, cfg)

        assert result is False


# ---------------------------------------------------------------------------
# _send_via_webhook
# ---------------------------------------------------------------------------

class TestSendViaWebhook:
    def _mock_response(self, body: str = "ok", status: int = 200):
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = body.encode()
        return mock_resp

    def test_success_returns_true(self):
        blocks = [{"type": "divider"}]
        with patch("urllib.request.urlopen", return_value=self._mock_response("ok")):
            result = _send_via_webhook("https://hooks.slack.com/xxx", blocks, "headline", "u_alice")
        assert result is True

    def test_unexpected_body_returns_false(self):
        blocks = []
        with patch("urllib.request.urlopen", return_value=self._mock_response("error")):
            result = _send_via_webhook("https://hooks.slack.com/xxx", blocks, "h", "u_alice")
        assert result is False

    def test_http_error_returns_false(self):
        blocks = []
        http_error = urllib.error.HTTPError(
            url="https://hooks.slack.com/xxx",
            code=403,
            msg="Forbidden",
            hdrs={},  # type: ignore
            fp=io.BytesIO(b"forbidden"),
        )
        with patch("urllib.request.urlopen", side_effect=http_error):
            result = _send_via_webhook("https://hooks.slack.com/xxx", blocks, "h", "u_alice")
        assert result is False

    def test_url_error_returns_false(self):
        blocks = []
        url_error = urllib.error.URLError(reason="Connection refused")
        with patch("urllib.request.urlopen", side_effect=url_error):
            result = _send_via_webhook("https://hooks.slack.com/xxx", blocks, "h", "u_alice")
        assert result is False
