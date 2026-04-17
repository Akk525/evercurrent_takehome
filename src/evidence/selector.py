"""
Structured evidence selection for summarization.

Instead of passing a raw text_bundle blob to the summarizer, this module
selects a compact evidence packet containing the most informative snippets.

This keeps summaries grounded, specific, and avoids hallucination from noisy
thread context. Both the FallbackProvider and any LLM provider should use this.

Evidence packet components:
    root_message        — First message in the thread (sets the context)
    key_technical_line  — Highest-information technical message (heuristic)
    blocker_indicator   — Strongest blocker/risk signal line (if any)
    latest_update       — Most recent non-root message
    entities            — Extracted entities (parts, revisions, suppliers, etc.)
    issue_context       — Brief cluster/status note if issue linking ran
    state_change        — State transition hint if detected
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Evidence packet data structure
# ---------------------------------------------------------------------------

@dataclass
class EvidencePacket:
    root_message: str = ""
    key_technical_line: str = ""
    blocker_indicator: str = ""
    latest_update: str = ""
    entities: dict[str, list[str]] = field(default_factory=dict)
    issue_context: str = ""
    state_change: str = ""

    def to_text(self) -> str:
        """Flatten to a concise text block for summarizer consumption."""
        parts: list[str] = []
        if self.root_message:
            parts.append(f"Context: {self.root_message}")
        if self.blocker_indicator and self.blocker_indicator != self.root_message:
            parts.append(f"Critical signal: {self.blocker_indicator}")
        if self.key_technical_line and self.key_technical_line not in (
            self.root_message, self.blocker_indicator
        ):
            parts.append(f"Technical detail: {self.key_technical_line}")
        if self.latest_update and self.latest_update not in (
            self.root_message, self.blocker_indicator, self.key_technical_line
        ):
            parts.append(f"Latest: {self.latest_update}")
        if self.entities:
            flat = []
            for etype, elist in self.entities.items():
                if elist:
                    flat.append(f"{etype}: {', '.join(elist[:3])}")
            if flat:
                parts.append("Entities: " + "; ".join(flat))
        if self.state_change:
            parts.append(f"State: {self.state_change}")
        if self.issue_context:
            parts.append(f"Issue: {self.issue_context}")
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Heuristic weights for line selection
# ---------------------------------------------------------------------------

BLOCKER_TERMS = [
    "blocked", "blocking", "cannot", "fail", "failed", "hang", "stuck",
    "100%", "all units", "critical", "stops", "prevents",
]
TECHNICAL_TERMS = [
    "register", "I2C", "SPI", "UART", "firmware", "voltage", "current",
    "ADC", "GPIO", "thermal", "PCB", "trace", "pull-up", "footprint",
    "sequence", "timing", "initialization", "PMIC", "BMS", "flash",
    "connector", "soldering", "BOM", "supplier", "lead time",
]


def _score_line(line: str) -> tuple[float, float]:
    """Return (blocker_score, technical_score) for a line."""
    lower = line.lower()
    blocker = sum(1 for t in BLOCKER_TERMS if t in lower) / len(BLOCKER_TERMS)
    technical = sum(1 for t in TECHNICAL_TERMS if t in lower) / len(TECHNICAL_TERMS)
    # Prefer longer lines (more informative) up to 200 chars
    length_bonus = min(len(line) / 200.0, 0.1)
    return blocker + length_bonus, technical + length_bonus


def _strip_prefix(line: str) -> str:
    """Remove '[user_id]: ' prefix from text_bundle lines."""
    if "]: " in line:
        return line.split("]: ", 1)[1].strip()
    return line.strip()


# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------

def build_evidence_packet(event) -> EvidencePacket:  # event: CandidateEvent
    """
    Build a structured evidence packet from a CandidateEvent.

    Selects the most informative snippets from the text_bundle,
    attaches entity metadata and any issue/state context.
    """
    lines = [
        _strip_prefix(line)
        for line in event.text_bundle.split("\n")
        if line.strip()
    ]

    if not lines:
        return EvidencePacket()

    root = lines[0]
    latest = lines[-1] if len(lines) > 1 else root

    # Score all non-root lines for blocker and technical content
    best_blocker = ""
    best_blocker_score = 0.0
    best_technical = ""
    best_technical_score = 0.0

    for line in lines[1:]:
        b_score, t_score = _score_line(line)
        if b_score > best_blocker_score:
            best_blocker_score = b_score
            best_blocker = line
        if t_score > best_technical_score:
            best_technical_score = t_score
            best_technical = line

    # Fallback: if no blocker line found, check root
    if not best_blocker:
        b_score, _ = _score_line(root)
        if b_score > 0:
            best_blocker = root

    # Entities from signals
    entities: dict[str, list[str]] = {}
    if event.signals and event.signals.extracted_entities:
        entities = {
            k: v for k, v in event.signals.extracted_entities.items() if v
        }

    # Issue context
    issue_context = ""
    if hasattr(event, "issue_cluster_id") and event.issue_cluster_id:
        related_count = len(getattr(event, "related_event_ids", []))
        status = getattr(event, "issue_status", "")
        if related_count > 0:
            issue_context = (
                f"{related_count} related thread(s) on this issue; status: {status}"
            )
        else:
            issue_context = f"status: {status}"

    # State change
    state_change = ""
    if event.signals and event.signals.state_change_hint:
        state_change = event.signals.state_change_hint

    return EvidencePacket(
        root_message=root,
        key_technical_line=best_technical,
        blocker_indicator=best_blocker,
        latest_update=latest,
        entities=entities,
        issue_context=issue_context,
        state_change=state_change,
    )
