"""
LLM provider abstraction.

The engine calls LLMProvider.summarize(). In prod you'd swap in a real provider.
FallbackProvider works without any API access and produces rule-based summaries.
"""

from __future__ import annotations

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

    def _build_summary(self, event: CandidateEvent, item: RankedDigestItem) -> str:
        signals = event.signals
        if signals is None:
            return "A discussion thread with limited signal."

        event_type = signals.dominant_event_type
        participants = len(event.participant_ids)
        messages = event.message_count

        type_phrases = {
            "blocker": "An active blocking issue requiring resolution",
            "risk": "A potential risk or concern",
            "decision": "A decision or alignment discussion",
            "status_update": "A status update or progress report",
            "request_for_input": "A request for input or feedback",
            "noise": "A social or low-signal discussion",
        }

        base = type_phrases.get(event_type, "A discussion thread")
        topic_str = (
            ", ".join(signals.topic_labels[:3]) if signals.topic_labels else "general"
        )

        urgency_phrase = ""
        if signals.urgency_score > 0.6:
            urgency_phrase = " Appears time-sensitive."
        elif signals.urgency_score > 0.3:
            urgency_phrase = " Some urgency signals present."

        unresolved_phrase = ""
        if signals.unresolved_score > 0.5:
            unresolved_phrase = " Thread appears unresolved."

        return (
            f"{base} covering topics: {topic_str}. "
            f"Thread has {messages} messages from {participants} participant(s)."
            f"{urgency_phrase}{unresolved_phrase}"
        )

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
            reasons.append("this is a relatively new or distinct topic")

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


def _build_prompt(
    event: CandidateEvent,
    item: RankedDigestItem,
    profile: UserContextProfile,
) -> str:
    signals = event.signals
    topic_str = ", ".join(signals.topic_labels) if signals and signals.topic_labels else "unknown"
    event_type = signals.dominant_event_type if signals else "unknown"
    urgency = signals.urgency_score if signals else 0.0

    return f"""You are generating a daily digest summary for an engineer on a hardware team.

Analyze the following Slack thread and produce a JSON response with exactly two fields:
- "summary": a 1-2 sentence plain English summary of what this thread is about and what the current state is
- "why_shown": a 1-2 sentence explanation of why this was shown to this user (be specific, not generic)

Guidelines:
- Use probabilistic language ("appears", "likely", "suggests") — do not state things as facts
- Keep each field under 80 words
- Focus on the engineering substance, not the social dynamics
- Do not invent details not present in the thread

Thread text:
---
{event.text_bundle[:1500]}
---

Thread metadata:
- Event type: {event_type}
- Topics: {topic_str}
- Urgency score: {urgency:.2f}
- Participants: {len(event.participant_ids)} people

User profile context:
- User active in channels: {', '.join(profile.active_channel_ids[:4])}
- User top topics: {', '.join(list(profile.topic_affinities.keys())[:4])}
- User relevance score for this thread: {item.score:.2f}

Respond with only valid JSON, no markdown.
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
