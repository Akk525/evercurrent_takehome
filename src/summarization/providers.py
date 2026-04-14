"""
LLM provider abstraction.

The engine calls LLMProvider.summarize(). In prod you'd swap in a real provider.
FallbackProvider works without any API access and produces rule-based summaries.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
import json

from src.models import CandidateEvent, RankedDigestItem, UserContextProfile


class LLMProvider(ABC):
    """Abstract interface for LLM summarization."""

    @abstractmethod
    def summarize(
        self,
        event: CandidateEvent,
        item: RankedDigestItem,
        profile: UserContextProfile,
    ) -> tuple[str, str]:
        """
        Returns (summary, why_shown).
        """
        ...

    @abstractmethod
    def summarize_shared(self, event: CandidateEvent) -> str:
        """
        Generate the shared part of the summary (situation + impact + resolution).
        Does NOT include per-user personalisation (why_shown).
        Called once per unique event; result reused across all users who see it.
        Returns a summary string only.
        """
        ...


class FallbackProvider(LLMProvider):
    """
    Rule-based fallback summarizer. No API calls.
    Used when LLM is unavailable or in test mode.
    """

    def summarize(
        self,
        event: CandidateEvent,
        item: RankedDigestItem,
        profile: UserContextProfile,
    ) -> tuple[str, str]:
        summary = self._build_summary(event, item)
        why_shown = self._build_why_shown(item, profile)
        return summary, why_shown

    def summarize_shared(self, event: CandidateEvent) -> str:
        """
        Shared summary: situation + impact + resolution, no per-user context.
        Delegates to _build_summary with a None item (item is not actually used).
        """
        return self._build_summary(event, None)  # type: ignore[arg-type]

    def _build_summary(self, event: CandidateEvent, item: RankedDigestItem) -> str:
        """
        Structured summary format: Situation. Impact. Resolution / next step.

        Uses concrete phrases extracted from the thread text rather than
        generic template strings. Aim: briefing-style, not form-letter style.
        """
        signals = event.signals
        if signals is None:
            return "A discussion thread with limited signal."

        event_type = signals.dominant_event_type
        participants = len(event.participant_ids)

        # Extract a concrete anchor phrase from the thread text
        key_phrase = _extract_key_phrase(event.text_bundle, event_type)

        # 1. Situation — grounded in actual thread content
        if event_type == "blocker":
            situation = f"A blocking issue has been raised: {key_phrase}"
        elif event_type == "risk":
            situation = f"A risk has been flagged: {key_phrase}"
        elif event_type == "decision":
            situation = f"The team appears to be working toward a decision: {key_phrase}"
        elif event_type == "status_update":
            situation = f"A status update was shared: {key_phrase}"
        elif event_type == "request_for_input":
            situation = f"Input has been requested: {key_phrase}"
        elif event_type == "noise":
            situation = key_phrase or "A social or off-topic thread is active"
        else:
            situation = f"A discussion is underway: {key_phrase}"

        # 2. Impact — tied to urgency and type
        if event_type in ("blocker", "risk") and signals.urgency_score > 0.6:
            impact = "High-urgency signals suggest this may affect near-term build progress."
        elif event_type in ("blocker", "risk") and signals.urgency_score > 0.3:
            impact = "This likely affects downstream work or upcoming milestones."
        elif event_type == "decision":
            impact = "The outcome likely affects multiple team members or workstreams."
        elif event_type == "request_for_input":
            impact = "A response from specific team members may be needed."
        else:
            impact = ""

        # 3. Resolution status
        if signals.unresolved_score > 0.5:
            resolution = (
                f"Appears unresolved — {participants} participant(s) involved, "
                f"{event.message_count} messages, no clear closure detected."
            )
        elif event_type in ("decision", "status_update") and signals.unresolved_score < 0.3:
            resolution = "Thread appears to be reaching or has reached a conclusion."
        else:
            resolution = (
                f"Ongoing — {participants} participant(s), {event.message_count} messages."
            )

        parts = [situation + "."]
        if impact:
            parts.append(impact)
        parts.append(resolution)

        return " ".join(parts)

    def _build_why_shown(
        self,
        item: RankedDigestItem,
        profile: UserContextProfile,
    ) -> str:
        features = item.reason_features
        reasons = []

        if features.user_affinity > 0.5:
            reasons.append("you are directly involved in this thread")
        elif features.user_affinity > 0.25:
            reasons.append("this thread involves your channels or frequent collaborators")

        if features.importance > 0.5:
            reasons.append("it carries a high importance signal")

        if features.urgency > 0.5:
            reasons.append("urgency signals suggest time-sensitive action may be needed")

        if features.momentum > 0.6:
            reasons.append("the thread has high recent activity")

        if features.novelty > 0.7:
            reasons.append("this appears to be a new or distinct topic")

        if features.embedding_affinity > 0.2:
            reasons.append("the content is semantically similar to your recent areas of focus")

        if not reasons:
            reasons.append("it scored in the top items for your profile today")

        return "Shown because " + ", and ".join(reasons) + "."


class GeminiProvider(LLMProvider):
    """
    Google Gemini-based summarization.
    Requires: pip install google-genai; GEMINI_API_KEY env var.
    """

    def __init__(self, model: str = "gemini-3-flash-preview"):
        try:
            from google import genai

            self._client = genai.Client()
            self._model = model
        except ImportError:
            raise RuntimeError(
                "google-genai package not installed. Run: pip install google-genai"
            )

    def summarize(
        self,
        event: CandidateEvent,
        item: RankedDigestItem,
        profile: UserContextProfile,
    ) -> tuple[str, str]:
        prompt = _build_prompt(event, item, profile)
        response = self._client.models.generate_content(
            model=self._model,
            contents=prompt,
        )
        return _parse_response(response.text or "")

    def summarize_shared(self, event: CandidateEvent) -> str:
        """Generate only the shared summary via LLM (no per-user why_shown)."""
        prompt = _build_shared_prompt(event)
        response = self._client.models.generate_content(
            model=self._model,
            contents=prompt,
        )
        return _parse_shared_response(response.text or "")


def _build_prompt(
    event: CandidateEvent,
    item: RankedDigestItem,
    profile: UserContextProfile,
) -> str:
    signals = event.signals
    topic_str = ", ".join(signals.topic_labels) if signals and signals.topic_labels else "unknown"
    event_type = signals.dominant_event_type if signals else "unknown"
    urgency = signals.urgency_score if signals else 0.0

    unresolved_str = "yes" if (signals and signals.unresolved_score > 0.5) else "no"
    top_topics = list(profile.topic_affinities.keys())[:4]

    return f"""You are generating a concise daily digest summary for an engineer on a hardware engineering team.

Analyze the following Slack thread and produce a JSON response with exactly two fields:

- "summary": 2-3 sentences structured as:
    1. What is happening (situation)
    2. Why it matters (impact on the project or team)
    3. Current status or next step (if visible from the thread)
  Use probabilistic language ("appears", "likely", "suggests") — do not state things as facts.
  Do not invent details not present in the thread.
  Focus on engineering substance, not social dynamics.
  Keep under 90 words.

- "why_shown": 1-2 sentences explaining why this was selected for this specific user.
  Reference their active topics or channels if relevant. Be specific, not generic.
  Keep under 50 words.

Thread text:
---
{event.text_bundle[:1500]}
---

Thread metadata:
- Event type: {event_type}
- Topics: {topic_str}
- Urgency: {urgency:.2f}/1.0
- Appears unresolved: {unresolved_str}
- Participants: {len(event.participant_ids)}

User profile:
- Active channels: {', '.join(profile.active_channel_ids[:4])}
- Top topics: {', '.join(top_topics) if top_topics else 'unknown'}
- Relevance score for this thread: {item.score:.2f}/1.0

Respond with only valid JSON. No markdown, no code fences.
"""


def _parse_response(text: str) -> tuple[str, str]:
    """Parse LLM JSON response, with graceful fallback."""
    try:
        data = json.loads(text.strip())
        return data.get("summary", ""), data.get("why_shown", "")
    except json.JSONDecodeError:
        # LLM returned something non-JSON — extract best effort
        lines = text.strip().split("\n")
        summary = lines[0] if lines else "Summary unavailable."
        why = lines[1] if len(lines) > 1 else "Relevance details unavailable."
        return summary, why


def _build_shared_prompt(event: CandidateEvent) -> str:
    """Build a prompt asking only for the shared event summary (no user context)."""
    signals = event.signals
    topic_str = ", ".join(signals.topic_labels) if signals and signals.topic_labels else "unknown"
    event_type = signals.dominant_event_type if signals else "unknown"
    urgency = signals.urgency_score if signals else 0.0
    unresolved_str = "yes" if (signals and signals.unresolved_score > 0.5) else "no"

    return f"""You are generating a concise daily digest summary for a hardware engineering team.

Analyze the following Slack thread and produce a JSON response with exactly one field:

- "summary": 2-3 sentences structured as:
    1. What is happening (situation)
    2. Why it matters (impact on the project or team)
    3. Current status (if visible from the thread)
  Use probabilistic language ("appears", "likely", "suggests").
  Do not invent details. Focus on engineering substance. Keep under 90 words.

Thread text:
---
{event.text_bundle[:1500]}
---

Thread metadata:
- Event type: {event_type}
- Topics: {topic_str}
- Urgency: {urgency:.2f}/1.0
- Appears unresolved: {unresolved_str}
- Participants: {len(event.participant_ids)}

Respond with only valid JSON. No markdown, no code fences.
"""


def _parse_shared_response(text: str) -> str:
    """Parse a shared-summary-only LLM response, with graceful fallback."""
    try:
        data = json.loads(text.strip())
        return data.get("summary", "Summary unavailable.")
    except json.JSONDecodeError:
        lines = text.strip().split("\n")
        return lines[0] if lines else "Summary unavailable."


# ---------------------------------------------------------------------------
# Fallback summarizer helpers
# ---------------------------------------------------------------------------

def _extract_key_phrase(text_bundle: str, event_type: str) -> str:
    """
    Extract the most informative concrete phrase from an event's text bundle.

    Strategy:
    1. Parse lines from the text bundle (each is "[user_id]: message text")
    2. Score each line for informativeness:
       - prefers lines with measurements, part names, version numbers, or
         technical keywords specific to the event type
       - penalises very short lines and pure questions
    3. Return the best sentence (first sentence of the best line), truncated.

    Falls back to the first line if no informative line is found.
    """
    lines = [l.strip() for l in text_bundle.split("\n") if l.strip()]
    if not lines:
        return "active discussion"

    # Strip "[user_id]: " prefix from each line
    clean_lines = []
    for line in lines:
        if "]: " in line:
            clean_lines.append(line.split("]: ", 1)[1].strip())
        else:
            clean_lines.append(line)

    # Patterns applied case-insensitively (keywords, abbreviations)
    ci_signals = [
        r"\d+%",                                                           # percentages
        r"\d+\s*(?:ms|us|hz|mhz|v|mv|mw|ma|°c|hours?|days?)",            # measurements + units
        r"(?:rev|revision)\s*[a-f]\b",                                     # hw revisions: Rev C
        r"\b(?:i2c|spi|uart|bms|pcb|adc|nor|ota|dma|gpio|imx|phy)\b",    # hw bus/block terms
        r"0x[0-9a-fA-F]+",                                                 # hex addresses
        r"\bstuck\b|\bfail(?:ure|s)?\b|\bblock(?:ed|ing)?\b|\bhang\b|\breset\b",
        r"\b(?:shortage|lead.?time|slip|out.?of.?spec)\b",
    ]
    # Pattern applied case-sensitively: all-caps identifiers (part numbers, constants)
    cs_part_number = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b")

    type_boosts: dict[str, list[str]] = {
        "blocker": ["stuck", "fail", "cannot", "block", "hang", "stops", "crash"],
        "risk": ["risk", "delay", "shortage", "slip", "concern", "lead time"],
        "decision": ["should we", "option", "recommend", "propose", "go with", "decide"],
        "status_update": ["update", "result", "confirmed", "completed", "landed"],
        "request_for_input": ["can you", "could you", "thoughts", "flagging", "looping"],
    }

    def score_line(line: str, position: int) -> float:
        if len(line) < 15:  # too short to be informative
            return 0.0
        s = 0.0
        lower = line.lower()
        # Case-insensitive keyword signals
        for pattern in ci_signals:
            if re.search(pattern, line, re.IGNORECASE):
                s += 0.3
        # Case-sensitive: all-caps part numbers / constants (SHT40, POWER_GOOD, MAX17261)
        if cs_part_number.search(line):
            s += 0.3
        # Event-type specific boost
        for kw in type_boosts.get(event_type, []):
            if kw in lower:
                s += 0.2
        # Prefer longer lines (more content) but cap benefit
        s += min(len(line) / 300.0, 0.2)
        # Penalise lines that are pure questions with no other content
        if line.strip().endswith("?") and len(line) < 60:
            s -= 0.2
        # Positional preference: root message (position 0) is the topic statement;
        # replies add context but are less likely to be the best standalone phrase.
        s -= position * 0.08
        return s

    scored = [(score_line(line, i), line) for i, line in enumerate(clean_lines)]
    best_score, best_line = max(scored, key=lambda x: x[0])

    # If no line stood out, fall back to the first clean line
    if best_score <= 0.0:
        best_line = clean_lines[0]

    # Take the first sentence of the best line
    sentence = re.split(r"(?<=[.!?])\s", best_line)[0].strip()
    # Strip trailing punctuation so callers can append their own
    sentence = sentence.rstrip(".!?").strip()
    # Truncate if too long
    if len(sentence) > 120:
        sentence = sentence[:117] + "..."

    return sentence
