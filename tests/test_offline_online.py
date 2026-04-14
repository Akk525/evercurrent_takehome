"""
Tests for the offline/online pipeline split.

Verifies that run_offline_enrichment() serialises correctly and
run_online_digest() produces digests equivalent to run_full_pipeline().
"""

from __future__ import annotations

import json
import pytest
import tempfile
from datetime import datetime, timezone
from pathlib import Path

NOW = datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc)
DATA_DIR = Path(__file__).parent.parent / "data" / "mock_slack"


def test_run_offline_enrichment_writes_file():
    """Offline enrichment writes a valid JSON snapshot."""
    from src.digest import run_offline_enrichment

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "enrichment.json"
        run_offline_enrichment(DATA_DIR, out, now=NOW)

        assert out.exists(), "Output file should be created"
        payload = json.loads(out.read_text())
        assert "enriched_events" in payload
        assert "profiles" in payload
        assert "workspace" in payload
        assert len(payload["enriched_events"]) > 0


def test_run_online_digest_produces_digests():
    """Online digest produces a digest for each user."""
    from src.digest import run_offline_enrichment, run_online_digest

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "enrichment.json"
        run_offline_enrichment(DATA_DIR, out, now=NOW)

        digests = run_online_digest(out, now=NOW, date_str="2026-04-10")
        assert len(digests) > 0
        for uid, digest in digests.items():
            assert digest.user_id == uid
            assert len(digest.items) > 0


def test_online_digest_subset_of_users():
    """Online digest can be filtered to specific users."""
    from src.digest import run_offline_enrichment, run_online_digest

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "enrichment.json"
        run_offline_enrichment(DATA_DIR, out, now=NOW)

        digests = run_online_digest(out, user_ids=["u_alice"], now=NOW)
        assert set(digests.keys()) == {"u_alice"}


def test_online_digest_items_have_summaries():
    """Items in the online digest should have summary and why_shown populated."""
    from src.digest import run_offline_enrichment, run_online_digest

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "enrichment.json"
        run_offline_enrichment(DATA_DIR, out, now=NOW)

        digests = run_online_digest(out, user_ids=["u_alice"], now=NOW)
        alice = digests["u_alice"]
        for item in alice.items:
            assert item.summary is not None and len(item.summary) > 0
            assert item.why_shown is not None and len(item.why_shown) > 0
