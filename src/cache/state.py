"""
Incremental processing state for the enrichment stage.

Tracks which CandidateEvents have already been enriched and whether they are
"dirty" (fingerprint changed → needs re-enrichment) or "clean" (skip).

Fingerprint is a deterministic MD5 hash of the stable fields that determine
whether enrichment outputs would change:
  - event_id
  - message_count
  - last_activity_at (ISO string)
  - unique_participant_count

If the fingerprint matches what's stored on disk, the event is clean.
If it doesn't match (or has never been seen), the event is dirty.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.models import CandidateEvent


@dataclass
class EventFingerprint:
    event_id: str
    fingerprint: str
    enriched_at: str  # ISO 8601 string


def compute_fingerprint(event: CandidateEvent) -> str:
    """
    Deterministic MD5 hex digest of the fields that affect enrichment output.

    If any of these change, the event is considered dirty and must be
    re-enriched.
    """
    raw = "|".join([
        event.event_id,
        str(event.message_count),
        event.last_activity_at.isoformat(),
        str(event.unique_participant_count),
    ])
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


class ProcessingState:
    """
    Tracks per-event enrichment fingerprints.

    - state_path=None → purely in-memory (no disk I/O).
    - state_path set → load/save to a JSON file on disk.

    Typical usage:
        state = ProcessingState(state_path=Path("cache/state.json"))
        state.load()
        enriched = enrich_candidate_events(events, workspace, now, processing_state=state)
        state.save()
    """

    def __init__(self, state_path: Optional[Path] = None) -> None:
        self._state_path = state_path
        # Maps event_id → EventFingerprint
        self._store: dict[str, EventFingerprint] = {}

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Load state from disk. No-op if path is None or file doesn't exist."""
        if self._state_path is None or not self._state_path.exists():
            return

        with self._state_path.open("r", encoding="utf-8") as fh:
            raw: dict = json.load(fh)

        self._store = {
            event_id: EventFingerprint(**entry)
            for event_id, entry in raw.items()
        }

    def save(self) -> None:
        """Persist state to disk. No-op if path is None."""
        if self._state_path is None:
            return

        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            event_id: asdict(fp)
            for event_id, fp in self._store.items()
        }
        with self._state_path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)

    # ------------------------------------------------------------------
    # Dirty / clean checks
    # ------------------------------------------------------------------

    def is_dirty(self, event: CandidateEvent) -> bool:
        """
        Returns True if the event has never been seen, or its fingerprint
        has changed since the last enrichment.
        """
        stored = self._store.get(event.event_id)
        if stored is None:
            return True
        return stored.fingerprint != compute_fingerprint(event)

    def mark_clean(self, event: CandidateEvent) -> None:
        """Record the current fingerprint for this event (after enrichment)."""
        self._store[event.event_id] = EventFingerprint(
            event_id=event.event_id,
            fingerprint=compute_fingerprint(event),
            enriched_at=datetime.now(tz=timezone.utc).isoformat(),
        )

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats(self, events: Optional[list[CandidateEvent]] = None) -> dict:
        """
        Returns {"total": N, "dirty": N, "clean": N}.

        If events is provided, counts are computed relative to that list.
        Otherwise uses the internal store counts (all tracked as clean).
        """
        if events is None:
            total = len(self._store)
            return {"total": total, "dirty": 0, "clean": total}

        total = len(events)
        dirty = sum(1 for e in events if self.is_dirty(e))
        clean = total - dirty
        return {"total": total, "dirty": dirty, "clean": clean}
