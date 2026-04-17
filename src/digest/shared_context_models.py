"""
Data models for the shared context and misalignment detection layer.

These are additive — they do not replace per-user digest models.
All inferences are probabilistic; hedged language is intentional.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class MisalignmentSignal(BaseModel):
    """A detected misalignment pattern for a candidate event."""

    event_id: str
    misalignment_flag: bool = False
    misalignment_reason: str = ""  # e.g. "Treated as high-urgency by firmware, low by supply chain"
    affected_function_ids: list[str] = Field(default_factory=list)   # user IDs in different camps
    differing_event_type_views: dict[str, str] = Field(default_factory=dict)  # user_id → their likely view
    confidence: float = 0.0


class SharedContextItem(BaseModel):
    """A globally critical item that all users should be aware of."""

    event_id: str
    title: str
    reason: str               # Why this is globally important
    signal_level: str         # "high" | "medium" | "low"
    event_type: str
    cross_functional_score: float = 0.0
    affected_user_ids: list[str] = Field(default_factory=list)  # Users who should know
    shared_context_score: float = 0.0  # [0, 1]


class SharedContextView(BaseModel):
    """Org-wide shared context: globally critical items + misalignments."""

    globally_critical: list[SharedContextItem] = Field(default_factory=list)
    cross_functional_hotspots: list[SharedContextItem] = Field(default_factory=list)
    misalignments: list[MisalignmentSignal] = Field(default_factory=list)
    generated_for_user_id: Optional[str] = None  # If personalized; None = org-wide
