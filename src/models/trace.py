"""
Per-event pipeline trace — captures inputs/outputs at each pipeline stage.

Assembled during _run_pipeline() and cached alongside enriched events.
Exposed via GET /api/events/{event_id}/trace for the decision traceability view.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class StageTrace(BaseModel):
    """One pipeline stage's contribution for a single event."""
    name: str           # internal key, e.g. "enrichment"
    label: str          # human label, e.g. "Semantic Enrichment"
    status: str         # "active" | "empty" | "skipped"
    outputs: dict[str, Any] = Field(default_factory=dict)
    score_delta: float = 0.0   # additive or multiplicative contribution to final score


class EventPipelineTrace(BaseModel):
    """Full pipeline trace for one candidate event."""
    event_id: str
    thread_id: str
    channel_id: str
    text_preview: str          # first 200 chars of text_bundle
    stages: list[StageTrace]
    final_score: float
    top_driver: str            # feature key with highest weighted contribution
    top_driver_value: float
    generated_at: str          # ISO datetime
