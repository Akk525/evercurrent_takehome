"""
Shared fixtures for all tests.
"""

from __future__ import annotations

import pytest
from datetime import datetime, timezone
from pathlib import Path

from src.ingest import load_workspace
from src.events import build_candidate_events
from src.enrichment import enrich_candidate_events
from src.profiles import build_user_profiles

DATA_DIR = Path(__file__).parent.parent / "data" / "mock_slack"
# Fixed clock so recency scores are deterministic in tests
NOW = datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="session")
def workspace():
    return load_workspace(DATA_DIR)


@pytest.fixture(scope="session")
def enriched_events(workspace):
    events = build_candidate_events(workspace)
    return enrich_candidate_events(events, workspace, now=NOW)


@pytest.fixture(scope="session")
def profiles(workspace, enriched_events):
    return build_user_profiles(workspace, enriched_events)


@pytest.fixture(scope="session")
def events_by_id(enriched_events):
    return {e.event_id: e for e in enriched_events}
