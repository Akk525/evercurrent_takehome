"""
Data models for ownership and accountability inference.

All inferences are probabilistic — never treat outputs as ground truth.
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from typing import Optional


class OwnershipSignals(BaseModel):
    """Inferred ownership and accountability for a CandidateEvent."""
    likely_owner_user_id: Optional[str] = None
    likely_owner_confidence: float = 0.0   # [0, 1]
    key_contributor_ids: list[str] = Field(default_factory=list)
    likely_function_or_team: Optional[str] = None  # e.g. "firmware", "supply_chain", "hardware"
    accountability_gap_flag: bool = False   # True when no clear owner is identifiable
    accountability_gap_reason: str = ""    # e.g. "no dominant contributor", "question unanswered"
    ownership_evidence: list[str] = Field(default_factory=list)  # Human-readable evidence strings
