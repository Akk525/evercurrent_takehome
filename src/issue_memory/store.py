"""
Persistent issue memory store — SQLite backed.

Each unique engineering issue gets a stable persistent_issue_id that survives
across pipeline runs and days. The matching strategy uses entity fingerprint
similarity + topic overlap to decide whether a current cluster maps to an
existing issue or is genuinely new.

Database: data/issue_memory.db (separate from digest_state.db)

Design notes:
    - issue_id is a UUID stable across runs
    - entity_fingerprint is a sorted, pipe-delimited string of high-value
      entities (parts, revisions, builds, suppliers) used for matching
    - status progression: new → ongoing → resurfacing | resolved
    - escalation_count increments when dominant_event_type worsens
      (status_update → risk → decision → blocker)
    - We intentionally do NOT store raw text or embeddings here
      (they're in the pipeline; persistence is for identity + timeline only)
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent.parent.parent / "data" / "issue_memory.db"

# Severity ordering for escalation detection
_TYPE_SEVERITY: dict[str, int] = {
    "noise": 0,
    "status_update": 1,
    "request_for_input": 2,
    "decision": 3,
    "risk": 4,
    "blocker": 5,
}


@dataclass
class IssueRecord:
    """A durable record for one persistent issue."""
    issue_id: str
    first_seen: str           # ISO datetime
    last_seen: str            # ISO datetime
    current_status: str       # "new" | "ongoing" | "resurfacing" | "resolved"
    prior_status: str         # previous status (for change detection)
    hours_open: float
    resurfaced_count: int
    escalation_count: int
    dominant_topic: str
    entity_fingerprint: str   # pipe-delimited sorted list of key entities
    related_thread_ids: str   # JSON list
    last_event_id: str
    last_title: str
    last_event_type: str      # dominant event type from last matching run
    updated_at: str           # ISO datetime

    def entity_set(self) -> set[str]:
        """Return the entity fingerprint as a set."""
        return set(e for e in self.entity_fingerprint.split("|") if e)

    def thread_ids(self) -> list[str]:
        try:
            return json.loads(self.related_thread_ids)
        except (json.JSONDecodeError, TypeError):
            return []

    def age_label(self) -> str:
        """Human-readable age string for digest display."""
        hours = self.hours_open
        if hours < 2:
            return "new today"
        if hours < 24:
            return f"{int(hours)}h old"
        days = int(hours / 24)
        return f"{days} day{'s' if days > 1 else ''} old"

    def memory_label(self) -> str:
        """Digest-facing label: 'Ongoing for 2 days', 'Resurfaced', etc."""
        if self.current_status == "new":
            return "New issue"
        if self.current_status == "resurfacing":
            suffix = f" ({self.resurfaced_count}× before)" if self.resurfaced_count > 0 else ""
            return f"Resurfaced{suffix}"
        if self.current_status == "resolved":
            return "Recently resolved"
        # ongoing
        return f"Ongoing — {self.age_label()}"

    def persistence_score(self) -> float:
        """
        Normalised persistence score [0, 1].
        Higher = issue has been open longer / resurfaced more.
        Saturates at 72h open or 3 resurfacings.
        """
        age_score = min(self.hours_open / 72.0, 1.0)
        resurface_score = min(self.resurfaced_count / 3.0, 1.0)
        return round(0.7 * age_score + 0.3 * resurface_score, 3)

    def escalation_score(self) -> float:
        """
        [0, 1] based on how severe the event type is and how many escalations.
        """
        severity = _TYPE_SEVERITY.get(self.last_event_type, 0) / max(_TYPE_SEVERITY.values())
        escalation = min(self.escalation_count / 3.0, 1.0)
        return round(0.6 * severity + 0.4 * escalation, 3)


class IssueMemoryStore:
    """
    SQLite-backed store for persistent issue memory.

    Usage:
        store = IssueMemoryStore()
        store.init()
        issues = store.load_all()
        store.upsert(record)
    """

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.init()

    def init(self) -> None:
        """Create tables if they don't exist. Idempotent."""
        con = sqlite3.connect(self.db_path)
        con.execute("""
            CREATE TABLE IF NOT EXISTS issues (
                issue_id           TEXT PRIMARY KEY,
                first_seen         TEXT NOT NULL,
                last_seen          TEXT NOT NULL,
                current_status     TEXT NOT NULL,
                prior_status       TEXT NOT NULL DEFAULT '',
                hours_open         REAL NOT NULL DEFAULT 0,
                resurfaced_count   INTEGER NOT NULL DEFAULT 0,
                escalation_count   INTEGER NOT NULL DEFAULT 0,
                dominant_topic     TEXT NOT NULL DEFAULT '',
                entity_fingerprint TEXT NOT NULL DEFAULT '',
                related_thread_ids TEXT NOT NULL DEFAULT '[]',
                last_event_id      TEXT NOT NULL DEFAULT '',
                last_title         TEXT NOT NULL DEFAULT '',
                last_event_type    TEXT NOT NULL DEFAULT 'noise',
                updated_at         TEXT NOT NULL
            )
        """)
        con.commit()
        con.close()

    def load_all(self) -> list[IssueRecord]:
        """Load all persisted issues."""
        con = sqlite3.connect(self.db_path)
        rows = con.execute(
            "SELECT issue_id, first_seen, last_seen, current_status, prior_status, "
            "hours_open, resurfaced_count, escalation_count, dominant_topic, "
            "entity_fingerprint, related_thread_ids, last_event_id, last_title, "
            "last_event_type, updated_at FROM issues ORDER BY last_seen DESC"
        ).fetchall()
        con.close()
        return [IssueRecord(*row) for row in rows]

    def upsert(self, record: IssueRecord) -> None:
        """Insert or replace an issue record."""
        con = sqlite3.connect(self.db_path)
        con.execute(
            """INSERT OR REPLACE INTO issues VALUES
               (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.issue_id,
                record.first_seen,
                record.last_seen,
                record.current_status,
                record.prior_status,
                record.hours_open,
                record.resurfaced_count,
                record.escalation_count,
                record.dominant_topic,
                record.entity_fingerprint,
                record.related_thread_ids,
                record.last_event_id,
                record.last_title,
                record.last_event_type,
                record.updated_at,
            ),
        )
        con.commit()
        con.close()

    def get(self, issue_id: str) -> Optional[IssueRecord]:
        con = sqlite3.connect(self.db_path)
        row = con.execute(
            "SELECT issue_id, first_seen, last_seen, current_status, prior_status, "
            "hours_open, resurfaced_count, escalation_count, dominant_topic, "
            "entity_fingerprint, related_thread_ids, last_event_id, last_title, "
            "last_event_type, updated_at FROM issues WHERE issue_id = ?",
            (issue_id,),
        ).fetchone()
        con.close()
        return IssueRecord(*row) if row else None


def make_entity_fingerprint(entities: dict[str, list[str]]) -> str:
    """
    Build a stable pipe-delimited entity fingerprint from extracted_entities.

    Only uses high-value discriminating entity types: parts, revisions, builds, suppliers.
    Sorted for stability across runs.
    """
    high_value = {"parts", "revisions", "builds", "suppliers"}
    flat: list[str] = []
    for etype, elist in entities.items():
        if etype in high_value:
            for e in elist:
                flat.append(e.lower().strip())
    return "|".join(sorted(set(flat)))


def new_issue_record(
    event_id: str,
    thread_ids: list[str],
    title: str,
    event_type: str,
    dominant_topic: str,
    entity_fingerprint: str,
    now: datetime,
) -> IssueRecord:
    """Create a brand-new IssueRecord for a cluster seen for the first time."""
    now_iso = now.isoformat()
    return IssueRecord(
        issue_id=str(uuid.uuid4()),
        first_seen=now_iso,
        last_seen=now_iso,
        current_status="new",
        prior_status="",
        hours_open=0.0,
        resurfaced_count=0,
        escalation_count=0,
        dominant_topic=dominant_topic,
        entity_fingerprint=entity_fingerprint,
        related_thread_ids=json.dumps(thread_ids),
        last_event_id=event_id,
        last_title=title,
        last_event_type=event_type,
        updated_at=now_iso,
    )
