"""
Tests for observability (observability/metrics.py).

Verifies StageTimer measures durations, PipelineMetrics accumulates counts,
and run_full_pipeline correctly populates metrics when passed in.
"""

from __future__ import annotations

import pytest
import time
from datetime import datetime, timezone
from pathlib import Path

from src.observability import PipelineMetrics, StageTimer

NOW = datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc)
DATA_DIR = Path(__file__).parent.parent / "data" / "mock_slack"


def test_stage_timer_measures_duration():
    """StageTimer.measure should record a non-zero duration."""
    with StageTimer.measure("test_stage") as t:
        pass  # near-instant

    assert t.name == "test_stage"
    assert t.duration_ms >= 0.0


def test_stage_timer_captures_work():
    """StageTimer duration should reflect actual elapsed time."""
    with StageTimer.measure("sleep_test") as t:
        time.sleep(0.05)  # 50 ms

    assert t.duration_ms >= 40.0, (
        f"Expected >= 40ms, got {t.duration_ms:.1f}ms"
    )


def test_pipeline_metrics_record_stage():
    """PipelineMetrics.record_stage should accumulate timer entries."""
    metrics = PipelineMetrics()
    with StageTimer.measure("stage_a") as t1:
        pass
    with StageTimer.measure("stage_b") as t2:
        pass

    metrics.record_stage(t1)
    metrics.record_stage(t2)

    assert len(metrics.stage_timers) == 2
    names = [s.name for s in metrics.stage_timers]
    assert "stage_a" in names
    assert "stage_b" in names


def test_pipeline_metrics_summary_dict():
    """summary_dict should return a nested dict with expected structure."""
    metrics = PipelineMetrics()
    metrics.total_candidate_events = 8
    metrics.events_enriched = 8
    metrics.users_processed = 6

    d = metrics.summary_dict()
    assert isinstance(d, dict)
    assert "events" in d
    assert d["events"]["total"] == 8
    assert d["events"]["enriched"] == 8
    assert d["ranking"]["users"] == 6


def test_run_full_pipeline_with_metrics():
    """
    run_full_pipeline should populate a PipelineMetrics object when passed in.
    """
    from src.digest import run_full_pipeline

    metrics = PipelineMetrics()
    digests = run_full_pipeline(
        data_dir=DATA_DIR,
        now=NOW,
        date_str="2026-04-10",
        metrics=metrics,
    )

    assert metrics.total_candidate_events > 0, "Should have counted candidate events"
    assert metrics.users_processed > 0, "Should have counted users"
    assert len(metrics.stage_timers) >= 3, "Should have recorded at least 3 stage timers"
    assert metrics.summaries_generated > 0, "Should have generated shared summaries"
