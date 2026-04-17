"""
Local Slack event store — SQLite backed.

Canonical working dataset for the digest engine when Slack integration is active.
The mock workspace JSON (data/mock_slack/) is used as the fallback when Slack
credentials are absent.

Schema overview:
    messages        — all ingested Slack messages (deduped by message_id)
    threads         — thread metadata + dirty/sync tracking
    channels        — channel metadata
    users           — user metadata
    ingest_events   — raw event envelope log for audit / dedup

Design:
    - Idempotent: re-inserting the same message_id is a no-op (INSERT OR IGNORE)
    - Updates: message edits use INSERT OR REPLACE on message_id
    - Dirty tracking: threads.is_dirty = 1 when replies may be missing
    - Sync state: last_synced_at tracks when we last fetched thread history
    - Deduplication: event_id column on ingest_events prevents processing the same Slack event twice
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent.parent.parent / "data" / "slack_ingest.db"


@dataclass
class StoredMessage:
    message_id: str        # Slack ts (e.g. "1234567890.123456")
    thread_id: str         # Slack thread_ts
    channel_id: str
    user_id: str
    text: str
    timestamp: str         # ISO datetime
    is_thread_root: bool
    is_deleted: bool = False
    is_edited: bool = False
    reaction_counts: str = "{}"   # JSON
    mentions: str = "[]"          # JSON


@dataclass
class StoredThread:
    thread_id: str
    channel_id: str
    root_message_id: str
    participant_ids: str   # JSON list
    message_ids: str       # JSON list
    started_at: str        # ISO datetime
    last_activity_at: str  # ISO datetime
    reply_count: int
    is_dirty: bool         # True = replies may be incomplete; reconciler should fetch
    is_complete: bool      # True = we have all replies (confirmed by reconciler)
    last_synced_at: Optional[str] = None  # When reconciler last fetched this thread


@dataclass
class StoredChannel:
    channel_id: str
    name: str
    topic: Optional[str]
    member_ids: str        # JSON list
    last_known_ts: Optional[str] = None   # Latest message ts seen — used for backfill cursors


@dataclass
class StoredUser:
    user_id: str
    display_name: str
    real_name: Optional[str]
    email: Optional[str]
    is_bot: bool = False


class SlackIngestStore:
    """
    SQLite-backed store for Slack ingestion data.

    Thread safety: each method opens and closes its own connection.
    Not designed for concurrent writers — use from a single async task or process.
    """

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def init(self) -> None:
        """Create tables if they don't exist. Idempotent."""
        con = sqlite3.connect(self.db_path)
        con.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                message_id      TEXT PRIMARY KEY,
                thread_id       TEXT NOT NULL,
                channel_id      TEXT NOT NULL,
                user_id         TEXT NOT NULL DEFAULT '',
                text            TEXT NOT NULL DEFAULT '',
                timestamp       TEXT NOT NULL,
                is_thread_root  INTEGER NOT NULL DEFAULT 0,
                is_deleted      INTEGER NOT NULL DEFAULT 0,
                is_edited       INTEGER NOT NULL DEFAULT 0,
                reaction_counts TEXT NOT NULL DEFAULT '{}',
                mentions        TEXT NOT NULL DEFAULT '[]'
            );

            CREATE TABLE IF NOT EXISTS threads (
                thread_id        TEXT PRIMARY KEY,
                channel_id       TEXT NOT NULL,
                root_message_id  TEXT NOT NULL,
                participant_ids  TEXT NOT NULL DEFAULT '[]',
                message_ids      TEXT NOT NULL DEFAULT '[]',
                started_at       TEXT NOT NULL,
                last_activity_at TEXT NOT NULL,
                reply_count      INTEGER NOT NULL DEFAULT 0,
                is_dirty         INTEGER NOT NULL DEFAULT 0,
                is_complete      INTEGER NOT NULL DEFAULT 0,
                last_synced_at   TEXT
            );

            CREATE TABLE IF NOT EXISTS channels (
                channel_id    TEXT PRIMARY KEY,
                name          TEXT NOT NULL DEFAULT '',
                topic         TEXT,
                member_ids    TEXT NOT NULL DEFAULT '[]',
                last_known_ts TEXT
            );

            CREATE TABLE IF NOT EXISTS users (
                user_id      TEXT PRIMARY KEY,
                display_name TEXT NOT NULL DEFAULT '',
                real_name    TEXT,
                email        TEXT,
                is_bot       INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS ingest_events (
                event_id    TEXT PRIMARY KEY,
                event_type  TEXT NOT NULL,
                received_at TEXT NOT NULL,
                payload     TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id);
            CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel_id);
            CREATE INDEX IF NOT EXISTS idx_threads_dirty ON threads(is_dirty);
        """)
        con.commit()
        con.close()

    # ------------------------------------------------------------------
    # Deduplication: ingest event log
    # ------------------------------------------------------------------

    def has_event(self, event_id: str) -> bool:
        """Return True if this Slack event_id has already been processed."""
        con = sqlite3.connect(self.db_path)
        row = con.execute(
            "SELECT 1 FROM ingest_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        con.close()
        return row is not None

    def record_event(self, event_id: str, event_type: str, payload: dict) -> None:
        """Persist an event envelope for audit and deduplication."""
        now = datetime.now(tz=timezone.utc).isoformat()
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT OR IGNORE INTO ingest_events VALUES (?, ?, ?, ?)",
            (event_id, event_type, now, json.dumps(payload)),
        )
        con.commit()
        con.close()

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    def upsert_message(self, msg: StoredMessage) -> None:
        """Insert or replace a message. Idempotent — safe to call on re-delivery."""
        con = sqlite3.connect(self.db_path)
        con.execute(
            """INSERT OR REPLACE INTO messages VALUES
               (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                msg.message_id,
                msg.thread_id,
                msg.channel_id,
                msg.user_id,
                msg.text,
                msg.timestamp,
                int(msg.is_thread_root),
                int(msg.is_deleted),
                int(msg.is_edited),
                msg.reaction_counts,
                msg.mentions,
            ),
        )
        con.commit()
        con.close()

    def mark_message_deleted(self, message_id: str) -> None:
        con = sqlite3.connect(self.db_path)
        con.execute(
            "UPDATE messages SET is_deleted = 1 WHERE message_id = ?", (message_id,)
        )
        con.commit()
        con.close()

    def get_messages_for_thread(self, thread_id: str) -> list[StoredMessage]:
        con = sqlite3.connect(self.db_path)
        rows = con.execute(
            """SELECT message_id, thread_id, channel_id, user_id, text, timestamp,
                      is_thread_root, is_deleted, is_edited, reaction_counts, mentions
               FROM messages WHERE thread_id = ? AND is_deleted = 0
               ORDER BY timestamp ASC""",
            (thread_id,),
        ).fetchall()
        con.close()
        return [StoredMessage(*r) for r in rows]

    def get_messages_for_channel(
        self,
        channel_id: str,
        limit: int = 200,
        after_ts: Optional[str] = None,
    ) -> list[StoredMessage]:
        con = sqlite3.connect(self.db_path)
        if after_ts:
            rows = con.execute(
                """SELECT message_id, thread_id, channel_id, user_id, text, timestamp,
                          is_thread_root, is_deleted, is_edited, reaction_counts, mentions
                   FROM messages
                   WHERE channel_id = ? AND is_deleted = 0 AND timestamp > ?
                   ORDER BY timestamp DESC LIMIT ?""",
                (channel_id, after_ts, limit),
            ).fetchall()
        else:
            rows = con.execute(
                """SELECT message_id, thread_id, channel_id, user_id, text, timestamp,
                          is_thread_root, is_deleted, is_edited, reaction_counts, mentions
                   FROM messages
                   WHERE channel_id = ? AND is_deleted = 0
                   ORDER BY timestamp DESC LIMIT ?""",
                (channel_id, limit),
            ).fetchall()
        con.close()
        return [StoredMessage(*r) for r in rows]

    # ------------------------------------------------------------------
    # Threads
    # ------------------------------------------------------------------

    def upsert_thread(self, thread: StoredThread) -> None:
        con = sqlite3.connect(self.db_path)
        con.execute(
            """INSERT OR REPLACE INTO threads VALUES
               (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                thread.thread_id,
                thread.channel_id,
                thread.root_message_id,
                thread.participant_ids,
                thread.message_ids,
                thread.started_at,
                thread.last_activity_at,
                thread.reply_count,
                int(thread.is_dirty),
                int(thread.is_complete),
                thread.last_synced_at,
            ),
        )
        con.commit()
        con.close()

    def mark_thread_dirty(self, thread_id: str) -> None:
        """Signal that this thread has new activity and replies may be incomplete."""
        con = sqlite3.connect(self.db_path)
        con.execute(
            "UPDATE threads SET is_dirty = 1, is_complete = 0 WHERE thread_id = ?",
            (thread_id,),
        )
        con.commit()
        con.close()

    def mark_thread_clean(self, thread_id: str) -> None:
        """Called by the reconciler after successfully fetching all replies."""
        now = datetime.now(tz=timezone.utc).isoformat()
        con = sqlite3.connect(self.db_path)
        con.execute(
            "UPDATE threads SET is_dirty = 0, is_complete = 1, last_synced_at = ? WHERE thread_id = ?",
            (now, thread_id),
        )
        con.commit()
        con.close()

    def get_dirty_threads(self, limit: int = 10) -> list[tuple[str, str]]:
        """
        Return up to `limit` dirty threads as (thread_id, channel_id) tuples.
        Ordered by last_activity_at DESC — most recently active first.
        """
        con = sqlite3.connect(self.db_path)
        rows = con.execute(
            """SELECT thread_id, channel_id FROM threads
               WHERE is_dirty = 1
               ORDER BY last_activity_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        con.close()
        return [(r[0], r[1]) for r in rows]

    def get_thread(self, thread_id: str) -> Optional[StoredThread]:
        con = sqlite3.connect(self.db_path)
        row = con.execute(
            """SELECT thread_id, channel_id, root_message_id, participant_ids,
                      message_ids, started_at, last_activity_at, reply_count,
                      is_dirty, is_complete, last_synced_at
               FROM threads WHERE thread_id = ?""",
            (thread_id,),
        ).fetchone()
        con.close()
        if row is None:
            return None
        return StoredThread(
            thread_id=row[0], channel_id=row[1], root_message_id=row[2],
            participant_ids=row[3], message_ids=row[4],
            started_at=row[5], last_activity_at=row[6],
            reply_count=row[7], is_dirty=bool(row[8]), is_complete=bool(row[9]),
            last_synced_at=row[10],
        )

    def update_thread_activity(
        self,
        thread_id: str,
        last_activity_at: str,
        reply_count_delta: int = 1,
        new_participant: Optional[str] = None,
        new_message_id: Optional[str] = None,
    ) -> None:
        """Incrementally update a thread record when a new reply arrives."""
        con = sqlite3.connect(self.db_path)
        thread_row = con.execute(
            "SELECT participant_ids, message_ids, reply_count FROM threads WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()

        if thread_row is None:
            con.close()
            return

        participants = json.loads(thread_row[0])
        message_ids = json.loads(thread_row[1])
        reply_count = thread_row[2]

        if new_participant and new_participant not in participants:
            participants.append(new_participant)
        if new_message_id and new_message_id not in message_ids:
            message_ids.append(new_message_id)

        con.execute(
            """UPDATE threads
               SET participant_ids = ?, message_ids = ?, reply_count = ?,
                   last_activity_at = ?, is_dirty = 1
               WHERE thread_id = ?""",
            (
                json.dumps(participants),
                json.dumps(message_ids),
                reply_count + reply_count_delta,
                last_activity_at,
                thread_id,
            ),
        )
        con.commit()
        con.close()

    # ------------------------------------------------------------------
    # Channels
    # ------------------------------------------------------------------

    def upsert_channel(self, channel: StoredChannel) -> None:
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT OR REPLACE INTO channels VALUES (?, ?, ?, ?, ?)",
            (channel.channel_id, channel.name, channel.topic,
             channel.member_ids, channel.last_known_ts),
        )
        con.commit()
        con.close()

    def update_channel_cursor(self, channel_id: str, last_ts: str) -> None:
        """Track the latest message ts seen for this channel (for backfill cursors)."""
        con = sqlite3.connect(self.db_path)
        con.execute(
            "UPDATE channels SET last_known_ts = ? WHERE channel_id = ?",
            (last_ts, channel_id),
        )
        con.commit()
        con.close()

    def get_channel(self, channel_id: str) -> Optional[StoredChannel]:
        con = sqlite3.connect(self.db_path)
        row = con.execute(
            "SELECT channel_id, name, topic, member_ids, last_known_ts FROM channels WHERE channel_id = ?",
            (channel_id,),
        ).fetchone()
        con.close()
        if row is None:
            return None
        return StoredChannel(*row)

    def list_channels(self) -> list[StoredChannel]:
        con = sqlite3.connect(self.db_path)
        rows = con.execute(
            "SELECT channel_id, name, topic, member_ids, last_known_ts FROM channels"
        ).fetchall()
        con.close()
        return [StoredChannel(*r) for r in rows]

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    def upsert_user(self, user: StoredUser) -> None:
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT OR REPLACE INTO users VALUES (?, ?, ?, ?, ?)",
            (user.user_id, user.display_name, user.real_name, user.email, int(user.is_bot)),
        )
        con.commit()
        con.close()

    def get_user(self, user_id: str) -> Optional[StoredUser]:
        con = sqlite3.connect(self.db_path)
        row = con.execute(
            "SELECT user_id, display_name, real_name, email, is_bot FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        con.close()
        if row is None:
            return None
        return StoredUser(
            user_id=row[0], display_name=row[1], real_name=row[2],
            email=row[3], is_bot=bool(row[4]),
        )

    def list_users(self) -> list[StoredUser]:
        """Return all users in the store."""
        con = sqlite3.connect(self.db_path)
        rows = con.execute(
            "SELECT user_id, display_name, real_name, email, is_bot FROM users"
        ).fetchall()
        con.close()
        return [
            StoredUser(
                user_id=r[0], display_name=r[1], real_name=r[2],
                email=r[3], is_bot=bool(r[4]),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Threads (bulk)
    # ------------------------------------------------------------------

    def list_threads(self) -> list[StoredThread]:
        """Return all threads, ordered by last_activity_at DESC."""
        con = sqlite3.connect(self.db_path)
        rows = con.execute(
            """SELECT thread_id, channel_id, root_message_id, participant_ids,
                      message_ids, started_at, last_activity_at, reply_count,
                      is_dirty, is_complete, last_synced_at
               FROM threads
               ORDER BY last_activity_at DESC"""
        ).fetchall()
        con.close()
        return [
            StoredThread(
                thread_id=r[0], channel_id=r[1], root_message_id=r[2],
                participant_ids=r[3], message_ids=r[4],
                started_at=r[5], last_activity_at=r[6],
                reply_count=r[7], is_dirty=bool(r[8]), is_complete=bool(r[9]),
                last_synced_at=r[10],
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Messages (bulk)
    # ------------------------------------------------------------------

    def get_all_messages(self, exclude_deleted: bool = True) -> list[StoredMessage]:
        """Return all messages, optionally excluding deleted ones. Ordered by timestamp ASC."""
        con = sqlite3.connect(self.db_path)
        if exclude_deleted:
            rows = con.execute(
                """SELECT message_id, thread_id, channel_id, user_id, text, timestamp,
                          is_thread_root, is_deleted, is_edited, reaction_counts, mentions
                   FROM messages
                   WHERE is_deleted = 0
                   ORDER BY timestamp ASC"""
            ).fetchall()
        else:
            rows = con.execute(
                """SELECT message_id, thread_id, channel_id, user_id, text, timestamp,
                          is_thread_root, is_deleted, is_edited, reaction_counts, mentions
                   FROM messages
                   ORDER BY timestamp ASC"""
            ).fetchall()
        con.close()
        return [StoredMessage(*r) for r in rows]

    def has_data(self) -> bool:
        """Return True if the store has at least one message — used to detect if Slack integration has ingested data."""
        con = sqlite3.connect(self.db_path)
        row = con.execute("SELECT 1 FROM messages LIMIT 1").fetchone()
        con.close()
        return row is not None

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, int]:
        """Return row counts for each table — useful for observability."""
        con = sqlite3.connect(self.db_path)
        result = {}
        for table in ("messages", "threads", "channels", "users", "ingest_events"):
            row = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            result[table] = row[0] if row else 0
        dirty = con.execute("SELECT COUNT(*) FROM threads WHERE is_dirty = 1").fetchone()
        result["dirty_threads"] = dirty[0] if dirty else 0
        con.close()
        return result
