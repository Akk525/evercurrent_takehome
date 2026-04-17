"""
Tests for Slack ingestion layer.

Coverage:
    1. Event deduplication (idempotency by event_id)
    2. Dirty-thread marking when a reply arrives
    3. New root message creates a clean thread record
    4. Message deletion marks is_deleted
    5. Message edit updates text + marks dirty
    6. Mention extraction from text
    7. SlackIngestStore: init, upsert_message, upsert_thread, stats, get_dirty_threads
    8. process_slack_event: unknown event types handled gracefully
    9. Engine still works on local data without Slack (graceful degradation)
    10. UserIdentityMap: load from env, bidirectional lookup, empty fallback
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from src.slack_ingest.store import (
    SlackIngestStore,
    StoredMessage,
    StoredThread,
    StoredChannel,
    StoredUser,
)
from src.slack_ingest.events import process_slack_event, _extract_mentions
from src.slack_ingest.models import SlackEventEnvelope
from src.slack_ingest.mapping import UserIdentityMap
from src.slack_ingest.reconciler import ReconciliationWorker
from src.observability.slack_metrics import SlackIngestMetrics


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path: Path) -> SlackIngestStore:
    s = SlackIngestStore(db_path=tmp_path / "test_ingest.db")
    s.init()
    return s


@pytest.fixture
def metrics() -> SlackIngestMetrics:
    return SlackIngestMetrics()


def _make_envelope(
    event_id: str = "evt_001",
    event_type: str = "message",
    subtype: str | None = None,
    ts: str = "1712700000.000001",
    thread_ts: str | None = None,
    channel: str = "C001",
    user: str = "U_alice",
    text: str = "Hello world",
    extra: dict | None = None,
) -> SlackEventEnvelope:
    inner: dict = {
        "type": event_type,
        "ts": ts,
        "channel": channel,
        "user": user,
        "text": text,
    }
    if subtype:
        inner["subtype"] = subtype
    if thread_ts:
        inner["thread_ts"] = thread_ts
    if extra:
        inner.update(extra)
    return SlackEventEnvelope(
        type="event_callback",
        event_id=event_id,
        event_time=1712700000,
        event=inner,
    )


# ---------------------------------------------------------------------------
# SlackIngestStore — basic CRUD
# ---------------------------------------------------------------------------

class TestSlackIngestStore:
    def test_init_creates_tables(self, store):
        stats = store.stats()
        assert "messages" in stats
        assert "threads" in stats
        assert stats["messages"] == 0

    def test_upsert_message_and_retrieve(self, store):
        msg = StoredMessage(
            message_id="1000.001",
            thread_id="1000.001",
            channel_id="C001",
            user_id="U_alice",
            text="Hello",
            timestamp="2026-04-10T12:00:00+00:00",
            is_thread_root=True,
        )
        store.upsert_message(msg)
        messages = store.get_messages_for_thread("1000.001")
        assert len(messages) == 1
        assert messages[0].text == "Hello"

    def test_upsert_message_idempotent(self, store):
        msg = StoredMessage(
            message_id="1000.001",
            thread_id="1000.001",
            channel_id="C001",
            user_id="U_alice",
            text="Hello",
            timestamp="2026-04-10T12:00:00+00:00",
            is_thread_root=True,
        )
        store.upsert_message(msg)
        store.upsert_message(msg)  # Second call is a no-op (INSERT OR REPLACE)
        assert store.stats()["messages"] == 1

    def test_message_edit_updates_text(self, store):
        msg = StoredMessage(
            message_id="1000.001", thread_id="1000.001", channel_id="C001",
            user_id="U_alice", text="Original", timestamp="2026-04-10T12:00:00+00:00",
            is_thread_root=True,
        )
        store.upsert_message(msg)
        edited = StoredMessage(
            message_id="1000.001", thread_id="1000.001", channel_id="C001",
            user_id="U_alice", text="Edited", timestamp="2026-04-10T12:00:00+00:00",
            is_thread_root=True, is_edited=True,
        )
        store.upsert_message(edited)
        messages = store.get_messages_for_thread("1000.001")
        assert messages[0].text == "Edited"
        assert messages[0].is_edited

    def test_mark_message_deleted(self, store):
        msg = StoredMessage(
            message_id="1000.001", thread_id="1000.001", channel_id="C001",
            user_id="U_alice", text="Bye", timestamp="2026-04-10T12:00:00+00:00",
            is_thread_root=True,
        )
        store.upsert_message(msg)
        store.mark_message_deleted("1000.001")
        messages = store.get_messages_for_thread("1000.001")
        assert len(messages) == 0  # Deleted messages excluded

    def test_upsert_thread_and_mark_dirty(self, store):
        thread = StoredThread(
            thread_id="1000.001",
            channel_id="C001",
            root_message_id="1000.001",
            participant_ids='["U_alice"]',
            message_ids='["1000.001"]',
            started_at="2026-04-10T12:00:00+00:00",
            last_activity_at="2026-04-10T12:00:00+00:00",
            reply_count=0,
            is_dirty=False,
            is_complete=True,
        )
        store.upsert_thread(thread)
        assert store.get_thread("1000.001").is_dirty is False

        store.mark_thread_dirty("1000.001")
        assert store.get_thread("1000.001").is_dirty is True

    def test_get_dirty_threads_returns_subset(self, store):
        for i in range(5):
            thread = StoredThread(
                thread_id=f"1000.00{i}",
                channel_id="C001",
                root_message_id=f"1000.00{i}",
                participant_ids='[]',
                message_ids='[]',
                started_at="2026-04-10T12:00:00+00:00",
                last_activity_at=f"2026-04-10T12:0{i}:00+00:00",
                reply_count=0,
                is_dirty=(i % 2 == 0),  # 0, 2, 4 → dirty
                is_complete=True,
            )
            store.upsert_thread(thread)

        dirty = store.get_dirty_threads(limit=2)
        assert len(dirty) == 2  # Respects limit

    def test_mark_thread_clean(self, store):
        thread = StoredThread(
            thread_id="t1", channel_id="C001", root_message_id="t1",
            participant_ids='[]', message_ids='[]',
            started_at="2026-04-10T12:00:00+00:00",
            last_activity_at="2026-04-10T12:00:00+00:00",
            reply_count=0, is_dirty=True, is_complete=False,
        )
        store.upsert_thread(thread)
        store.mark_thread_clean("t1")
        t = store.get_thread("t1")
        assert t.is_dirty is False
        assert t.is_complete is True
        assert t.last_synced_at is not None

    def test_upsert_channel_and_cursor(self, store):
        ch = StoredChannel(channel_id="C001", name="hardware", topic="hw",
                           member_ids='["U_alice"]')
        store.upsert_channel(ch)
        store.update_channel_cursor("C001", "1712700100.000001")
        fetched = store.get_channel("C001")
        assert fetched.last_known_ts == "1712700100.000001"

    def test_upsert_user(self, store):
        user = StoredUser(user_id="U_alice", display_name="Alice", real_name="Alice Smith",
                          email="alice@example.com")
        store.upsert_user(user)
        fetched = store.get_user("U_alice")
        assert fetched.display_name == "Alice"

    def test_stats_counts_correctly(self, store):
        store.upsert_message(StoredMessage(
            message_id="m1", thread_id="t1", channel_id="C1",
            user_id="U1", text="hi", timestamp="2026-04-10T12:00:00+00:00",
            is_thread_root=True,
        ))
        stats = store.stats()
        assert stats["messages"] == 1


# ---------------------------------------------------------------------------
# process_slack_event — event routing
# ---------------------------------------------------------------------------

class TestProcessSlackEvent:
    def test_new_root_message_stored(self, store, metrics):
        env = _make_envelope(ts="1000.001")
        process_slack_event(env, store, metrics)
        msgs = store.get_messages_for_thread("1000.001")
        assert len(msgs) == 1
        assert msgs[0].is_thread_root

    def test_thread_reply_marks_dirty(self, store, metrics):
        # Create root thread first
        thread = StoredThread(
            thread_id="1000.001", channel_id="C001", root_message_id="1000.001",
            participant_ids='["U_alice"]', message_ids='["1000.001"]',
            started_at="2026-04-10T12:00:00+00:00",
            last_activity_at="2026-04-10T12:00:00+00:00",
            reply_count=0, is_dirty=False, is_complete=True,
        )
        store.upsert_thread(thread)

        # Send a reply
        reply_env = _make_envelope(
            event_id="evt_002",
            ts="1000.002",
            thread_ts="1000.001",
            user="U_bob",
            text="Reply to thread",
        )
        process_slack_event(reply_env, store, metrics)

        t = store.get_thread("1000.001")
        assert t is not None
        assert t.is_dirty is True
        assert metrics.dirty_threads_marked == 1

    def test_duplicate_event_skipped(self, store, metrics):
        env = _make_envelope(event_id="evt_001", ts="1000.001")
        processed_first = process_slack_event(env, store, metrics)
        processed_second = process_slack_event(env, store, metrics)

        assert processed_first is True
        assert processed_second is False
        assert metrics.events_deduplicated == 1
        # Only one message stored
        assert store.stats()["messages"] == 1

    def test_message_deleted_event(self, store, metrics):
        # Pre-populate message
        store.upsert_message(StoredMessage(
            message_id="1000.001", thread_id="1000.001", channel_id="C001",
            user_id="U_alice", text="Original", timestamp="2026-04-10T12:00:00+00:00",
            is_thread_root=True,
        ))

        env = _make_envelope(
            event_id="evt_del",
            subtype="message_deleted",
            extra={"deleted_ts": "1000.001"},
        )
        process_slack_event(env, store, metrics)

        msgs = store.get_messages_for_thread("1000.001")
        assert len(msgs) == 0

    def test_message_changed_event(self, store, metrics):
        # Pre-populate
        store.upsert_message(StoredMessage(
            message_id="1000.001", thread_id="1000.001", channel_id="C001",
            user_id="U_alice", text="Original", timestamp="2026-04-10T12:00:00+00:00",
            is_thread_root=True,
        ))
        store.upsert_thread(StoredThread(
            thread_id="1000.001", channel_id="C001", root_message_id="1000.001",
            participant_ids='[]', message_ids='["1000.001"]',
            started_at="2026-04-10T12:00:00+00:00",
            last_activity_at="2026-04-10T12:00:00+00:00",
            reply_count=0, is_dirty=False, is_complete=True,
        ))

        env = _make_envelope(
            event_id="evt_edit",
            subtype="message_changed",
            extra={"message": {"ts": "1000.001", "user": "U_alice", "text": "Edited text"}},
        )
        process_slack_event(env, store, metrics)

        msgs = store.get_messages_for_thread("1000.001")
        assert msgs[0].text == "Edited text"

    def test_unknown_event_type_handled_gracefully(self, store, metrics):
        env = SlackEventEnvelope(
            type="event_callback",
            event_id="evt_unknown",
            event={"type": "reaction_added", "user": "U1", "reaction": "thumbsup"},
        )
        result = process_slack_event(env, store, metrics)
        # Should not raise, should return False (unhandled but graceful)
        assert result is False

    def test_app_rate_limited_event(self, store, metrics):
        env = SlackEventEnvelope(
            type="event_callback",
            event_id="evt_rl",
            event={"type": "app_rate_limited", "api_app_id": "A001", "minute_rate_limited": 1234567890},
        )
        result = process_slack_event(env, store, metrics)
        assert result is True
        assert metrics.app_rate_limited_events == 1

    def test_no_signals_without_event(self, store, metrics):
        env = SlackEventEnvelope(type="event_callback", event_id="evt_empty", event=None)
        result = process_slack_event(env, store, metrics)
        assert result is False


# ---------------------------------------------------------------------------
# Mention extraction
# ---------------------------------------------------------------------------

class TestExtractMentions:
    def test_single_mention(self):
        assert _extract_mentions("<@U012AB3CD> please review") == ["U012AB3CD"]

    def test_multiple_mentions(self):
        result = _extract_mentions("<@U111> and <@U222> please help")
        assert "U111" in result
        assert "U222" in result

    def test_no_mentions(self):
        assert _extract_mentions("no mentions here") == []

    def test_empty_text(self):
        assert _extract_mentions("") == []

    def test_none_text(self):
        assert _extract_mentions(None) == []


# ---------------------------------------------------------------------------
# UserIdentityMap
# ---------------------------------------------------------------------------

class TestUserIdentityMap:
    def test_load_from_env(self):
        mapping = '{"u_alice": "U012", "u_bob": "U034"}'
        with patch.dict("os.environ", {"SLACK_USER_MAP": mapping}):
            identity = UserIdentityMap.load()
        assert identity.engine_to_slack("u_alice") == "U012"
        assert identity.slack_to_engine("U034") == "u_bob"

    def test_empty_when_unconfigured(self):
        with patch.dict("os.environ", {}, clear=True):
            with patch("src.slack_ingest.mapping._CONFIG_FILE") as mock_path:
                mock_path.exists.return_value = False
                identity = UserIdentityMap.load()
        assert identity.is_empty()
        assert identity.engine_to_slack("u_alice") is None

    def test_invalid_json_env_falls_back_gracefully(self):
        with patch.dict("os.environ", {"SLACK_USER_MAP": "not_json"}):
            with patch("src.slack_ingest.mapping._CONFIG_FILE") as mock_path:
                mock_path.exists.return_value = False
                identity = UserIdentityMap.load()
        assert identity.is_empty()

    def test_bidirectional_lookup(self):
        identity = UserIdentityMap({"u_alice": "U_SLACK_ALICE"})
        assert identity.engine_to_slack("u_alice") == "U_SLACK_ALICE"
        assert identity.slack_to_engine("U_SLACK_ALICE") == "u_alice"
        assert identity.engine_to_slack("u_unknown") is None

    def test_register_runtime(self):
        identity = UserIdentityMap({})
        identity.register("u_carlos", "U_CARLOS")
        assert identity.engine_to_slack("u_carlos") == "U_CARLOS"

    def test_all_engine_ids(self):
        identity = UserIdentityMap({"u_a": "S1", "u_b": "S2"})
        ids = identity.all_engine_ids()
        assert "u_a" in ids and "u_b" in ids

    def test_load_from_config_file(self, tmp_path):
        config_file = tmp_path / "slack_user_map.json"
        config_file.write_text(json.dumps({"u_alice": "U_FILE"}))
        with patch.dict("os.environ", {}, clear=True):
            with patch("src.slack_ingest.mapping._CONFIG_FILE", config_file):
                identity = UserIdentityMap.load()
        assert identity.engine_to_slack("u_alice") == "U_FILE"


# ---------------------------------------------------------------------------
# Graceful degradation — engine works without Slack
# ---------------------------------------------------------------------------

class TestGracefulDegradation:
    def test_local_pipeline_runs_without_slack_config(self, enriched_events, profiles):
        """Engine still produces digests when Slack env vars are absent."""
        from src.digest.assembler import assemble_digest
        from datetime import datetime, timezone

        assert enriched_events, "Need at least one event"
        uid = next(iter(profiles))
        profile = profiles[uid]
        events_by_id = {e.event_id: e for e in enriched_events}

        digest = assemble_digest(
            user_id=uid,
            enriched_events=enriched_events,
            profile=profile,
            events_by_id=events_by_id,
            top_k=3,
            now=datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc),
        )
        assert digest is not None
        assert digest.user_id == uid

    def test_delivery_config_returns_none_without_token(self):
        """load_config() returns None gracefully when SLACK_BOT_TOKEN is absent."""
        from src.slack_delivery.config import load_config
        with patch.dict("os.environ", {}, clear=True):
            config = load_config()
        assert config is None

    def test_socket_mode_manager_skips_gracefully(self, store):
        """SocketModeManager.start() exits cleanly without SLACK_APP_TOKEN."""
        import asyncio
        from src.slack_ingest.socket_mode import SocketModeManager
        from src.slack_ingest.rate_limits import RateLimiter

        manager = SocketModeManager(store=store, limiter=RateLimiter(), app_token="")
        assert not manager.is_configured()
        # Running start() should return immediately without blocking
        asyncio.get_event_loop().run_until_complete(manager.start())


# ---------------------------------------------------------------------------
# SlackStoreAdapter — store → SlackWorkspace bridge
# ---------------------------------------------------------------------------

class TestSlackStoreAdapter:
    """Tests for load_workspace_from_slack_store adapter."""

    @pytest.fixture
    def adapter_store(self, tmp_path: Path) -> SlackIngestStore:
        s = SlackIngestStore(db_path=tmp_path / "adapter_test.db")
        s.init()
        return s

    def test_empty_store_returns_empty_workspace(self, adapter_store):
        from src.slack_ingest.adapter import load_workspace_from_slack_store

        workspace = load_workspace_from_slack_store(adapter_store)

        assert workspace.messages == []
        assert workspace.threads == []
        assert workspace.channels == []
        assert workspace.users == []

    def test_messages_mapped_correctly(self, adapter_store):
        from src.slack_ingest.adapter import load_workspace_from_slack_store

        msg = StoredMessage(
            message_id="1712700000.000001",
            thread_id="1712700000.000001",
            channel_id="C001",
            user_id="U_alice",
            text="Board bring-up blocked on missing FPGA firmware.",
            timestamp="2026-04-10T12:00:00+00:00",
            is_thread_root=True,
            reaction_counts='{"eyes": 2}',
            mentions='["U_bob"]',
        )
        adapter_store.upsert_message(msg)

        workspace = load_workspace_from_slack_store(adapter_store)

        assert len(workspace.messages) == 1
        m = workspace.messages[0]
        assert m.message_id == "1712700000.000001"
        assert m.thread_id == "1712700000.000001"
        assert m.channel_id == "C001"
        assert m.user_id == "U_alice"
        assert m.text == "Board bring-up blocked on missing FPGA firmware."
        assert m.is_thread_root is True
        assert m.reaction_counts == {"eyes": 2}
        assert m.mentions == ["U_bob"]
        # Timestamp must be a datetime
        from datetime import datetime
        assert isinstance(m.timestamp, datetime)

    def test_threads_mapped_correctly(self, adapter_store):
        from src.slack_ingest.adapter import load_workspace_from_slack_store

        thread = StoredThread(
            thread_id="1712700000.000001",
            channel_id="C001",
            root_message_id="1712700000.000001",
            participant_ids='["U_alice", "U_bob"]',
            message_ids='["1712700000.000001", "1712700001.000001"]',
            started_at="2026-04-10T10:00:00+00:00",
            last_activity_at="2026-04-10T12:00:00+00:00",
            reply_count=3,
            is_dirty=False,
            is_complete=True,
        )
        adapter_store.upsert_thread(thread)

        workspace = load_workspace_from_slack_store(adapter_store)

        assert len(workspace.threads) == 1
        t = workspace.threads[0]
        assert t.thread_id == "1712700000.000001"
        assert t.channel_id == "C001"
        assert t.root_message_id == "1712700000.000001"
        assert t.participant_ids == ["U_alice", "U_bob"]
        assert t.message_ids == ["1712700000.000001", "1712700001.000001"]
        assert t.reply_count == 3
        from datetime import datetime
        assert isinstance(t.started_at, datetime)
        assert isinstance(t.last_activity_at, datetime)

    def test_users_mapped_correctly(self, adapter_store):
        from src.slack_ingest.adapter import load_workspace_from_slack_store

        user = StoredUser(
            user_id="U_alice",
            display_name="alice",
            real_name="Alice Smith",
            email="alice@example.com",
            is_bot=False,
        )
        adapter_store.upsert_user(user)

        workspace = load_workspace_from_slack_store(adapter_store)

        assert len(workspace.users) == 1
        u = workspace.users[0]
        assert u.user_id == "U_alice"
        # real_name takes precedence over display_name
        assert u.display_name == "Alice Smith"
        assert u.role is None

    def test_users_falls_back_to_display_name_when_no_real_name(self, adapter_store):
        from src.slack_ingest.adapter import load_workspace_from_slack_store

        user = StoredUser(
            user_id="U_bot",
            display_name="bot-user",
            real_name=None,
            email=None,
            is_bot=True,
        )
        adapter_store.upsert_user(user)

        workspace = load_workspace_from_slack_store(adapter_store)

        assert len(workspace.users) == 1
        assert workspace.users[0].display_name == "bot-user"

    def test_has_data_false_when_empty(self, adapter_store):
        assert adapter_store.has_data() is False

    def test_has_data_true_when_messages_present(self, adapter_store):
        msg = StoredMessage(
            message_id="9000.001",
            thread_id="9000.001",
            channel_id="C_test",
            user_id="U_test",
            text="test message",
            timestamp="2026-04-10T12:00:00+00:00",
            is_thread_root=True,
        )
        adapter_store.upsert_message(msg)
        assert adapter_store.has_data() is True
