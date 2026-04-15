"""
SQLite-backed persistence for DM messages and thread replies.

All writes go through here; in-memory stores are the primary read path.
The database is a single file at data/digest_state.db — no migrations needed,
CREATE TABLE IF NOT EXISTS is idempotent.
"""

import sqlite3
from collections import defaultdict
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "digest_state.db"


def init_db() -> None:
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS dm_messages (
            message_id TEXT PRIMARY KEY,
            sender_id  TEXT NOT NULL,
            other_id   TEXT NOT NULL,
            text       TEXT NOT NULL,
            timestamp  TEXT NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS thread_replies (
            message_id  TEXT PRIMARY KEY,
            thread_id   TEXT NOT NULL,
            channel_id  TEXT NOT NULL,
            user_id     TEXT NOT NULL,
            text        TEXT NOT NULL,
            timestamp   TEXT NOT NULL
        )
    """)
    con.commit()
    con.close()


def load_dm_messages() -> dict[str, list[dict]]:
    """Returns canonical_key -> list[dict] sorted by timestamp."""
    result: dict[str, list[dict]] = defaultdict(list)
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT message_id, sender_id, other_id, text, timestamp "
        "FROM dm_messages ORDER BY timestamp"
    ).fetchall()
    con.close()
    for message_id, sender_id, other_id, text, timestamp in rows:
        key = ":".join(sorted([sender_id, other_id]))
        result[key].append({
            "message_id": message_id,
            "sender_id": sender_id,
            "text": text,
            "timestamp": timestamp,
        })
    return result


def save_dm_message(sender_id: str, other_id: str, msg: dict) -> None:
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT OR IGNORE INTO dm_messages VALUES (?, ?, ?, ?, ?)",
        (msg["message_id"], sender_id, other_id, msg["text"], msg["timestamp"]),
    )
    con.commit()
    con.close()


def load_thread_replies() -> list[dict]:
    """Returns all user-posted thread replies sorted by timestamp."""
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT message_id, thread_id, channel_id, user_id, text, timestamp "
        "FROM thread_replies ORDER BY timestamp"
    ).fetchall()
    con.close()
    return [
        {
            "message_id": r[0],
            "thread_id": r[1],
            "channel_id": r[2],
            "user_id": r[3],
            "text": r[4],
            "timestamp": r[5],
        }
        for r in rows
    ]


def save_thread_reply(msg: dict) -> None:
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT OR IGNORE INTO thread_replies VALUES (?, ?, ?, ?, ?, ?)",
        (
            msg["message_id"],
            msg["thread_id"],
            msg["channel_id"],
            msg["user_id"],
            msg["text"],
            msg["timestamp"],
        ),
    )
    con.commit()
    con.close()
