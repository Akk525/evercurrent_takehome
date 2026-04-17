"""
Impact reasoning: produce a "why this matters" statement for a CandidateEvent.

Grounded in extracted entities, event type, topic labels, and issue memory context.
All output is probabilistic and uses hedged language ("likely", "may", "suggests").

Design:
    - Pure heuristic — no LLM required
    - Template-driven: select the most specific applicable template
    - Ordered by specificity: entity+type > type+topic > type-only > generic
    - Returns a single sentence, max ~120 chars
    - Returns "" for noise events (no impact statement warranted)
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Template selection
# ---------------------------------------------------------------------------

def build_impact_statement(
    event,  # CandidateEvent — typed loosely to avoid circular import
) -> str:
    """
    Generate a one-sentence impact statement for a candidate event.

    Returns an empty string for noise events or events with no signals.
    The statement is suitable for appending to summaries or why_shown text.
    """
    if event.signals is None:
        return ""

    signals = event.signals
    event_type = signals.dominant_event_type
    topics = signals.topic_labels
    entities = signals.extracted_entities or {}
    memory_signals = getattr(event, "issue_memory_signals", None)

    # Noise events don't warrant an impact statement
    if event_type == "noise":
        return ""

    # Extract entity sets
    parts = [p.upper() for p in entities.get("parts", [])][:3]
    revisions = entities.get("revisions", [])[:2]
    builds = [b for b in entities.get("builds", [])][:2]
    suppliers = entities.get("suppliers", [])[:2]

    # --- Specificity layer 1: entity + event type ---
    statement = _entity_type_template(event_type, parts, revisions, builds, suppliers, topics)
    if statement:
        return _hedge(statement)

    # --- Specificity layer 2: event type + topic ---
    statement = _type_topic_template(event_type, topics, memory_signals)
    if statement:
        return _hedge(statement)

    # --- Specificity layer 3: event type only ---
    return _hedge(_type_only_template(event_type))


# ---------------------------------------------------------------------------
# Template implementations
# ---------------------------------------------------------------------------

def _entity_type_template(
    event_type: str,
    parts: list[str],
    revisions: list[str],
    builds: list[str],
    suppliers: list[str],
    topics: list[str],
) -> str:
    """Most specific: anchored to named entities."""
    part_str = parts[0] if parts else ""
    rev_str = revisions[0] if revisions else ""
    build_str = builds[0] if builds else ""
    supplier_str = suppliers[0] if suppliers else ""

    if event_type == "blocker":
        if build_str and part_str:
            return f"This may block {build_str} validation if {part_str} is not resolved."
        if build_str:
            return f"This may block {build_str} validation."
        if part_str and rev_str:
            return f"{part_str} on {rev_str} appears blocked — downstream work may stall."
        if part_str:
            return f"{part_str} appears blocked — downstream work may stall."
        if supplier_str:
            return f"A supplier issue with {supplier_str} may block near-term build steps."

    elif event_type == "risk":
        if supplier_str and build_str:
            return f"{supplier_str} availability risk may delay {build_str} procurement."
        if supplier_str:
            return f"{supplier_str} availability risk may affect component lead times."
        if part_str and rev_str:
            return f"Risk on {part_str} ({rev_str}) may affect upcoming validation."
        if part_str and build_str:
            return f"Risk involving {part_str} may affect {build_str} readiness."
        if build_str:
            return f"This risk may delay {build_str} if not mitigated."

    elif event_type == "decision":
        if build_str and part_str:
            return f"The decision on {part_str} will shape {build_str} architecture."
        if part_str:
            return f"The decision on {part_str} will affect downstream integration work."
        if build_str:
            return f"This decision affects {build_str} scoping and team priorities."

    elif event_type == "request_for_input":
        if part_str or rev_str:
            entity = part_str or rev_str
            return f"Input on {entity} is needed to proceed — delays may cascade."
        if build_str:
            return f"A team response is needed to unblock {build_str} planning."

    elif event_type == "status_update":
        if part_str and rev_str:
            return f"Status update on {part_str} ({rev_str}) affects downstream scheduling."
        if build_str:
            return f"Build status for {build_str} affects scheduling for dependent teams."

    return ""


def _type_topic_template(
    event_type: str,
    topics: list[str],
    memory_signals,
) -> str:
    """Medium specificity: event type + topic label."""
    topic = topics[0] if topics else ""
    is_persistent = (
        memory_signals is not None
        and not memory_signals.is_new_issue
        and memory_signals.issue_persistence_score > 0.3
    )

    topic_impact: dict[str, dict[str, str]] = {
        "blocker": {
            "supply_chain": "Supply chain blockers typically affect multiple build phases.",
            "firmware": "Firmware blockers may stall integration and testing schedules.",
            "hardware": "Hardware blockers may delay EVT/DVT validation timelines.",
            "testing": "Test blockers prevent sign-off and may slip milestone dates.",
            "scheduling": "Schedule blockers risk cascading into downstream milestones.",
        },
        "risk": {
            "supply_chain": "Supply chain risks may affect component availability and cost.",
            "firmware": "Firmware risks may surface late in integration if not addressed early.",
            "hardware": "Hardware risks compound quickly once physical builds are in progress.",
            "testing": "Unresolved test risks may resurface during formal validation.",
            "scheduling": "Schedule risk affects team bandwidth and external commitments.",
        },
        "decision": {
            "supply_chain": "Supplier decisions lock in lead times and BOM cost.",
            "firmware": "Firmware architecture decisions affect long-term maintainability.",
            "hardware": "Hardware design decisions are expensive to reverse after tape-out.",
            "testing": "Test strategy decisions determine coverage and acceptable risk.",
        },
        "request_for_input": {
            "supply_chain": "Supplier input is needed to confirm component availability.",
            "firmware": "Firmware input is needed to unblock integration work.",
            "hardware": "Hardware input is required before design sign-off.",
        },
        "status_update": {
            "supply_chain": "Supply chain status affects BOM planning and procurement timing.",
            "firmware": "Firmware status informs integration scheduling.",
            "hardware": "Hardware status determines readiness for the next build phase.",
        },
    }

    base = topic_impact.get(event_type, {}).get(topic, "")
    if base and is_persistent:
        base = base.rstrip(".") + " — and this issue has been active for some time."
    return base


def _type_only_template(event_type: str) -> str:
    """Lowest specificity: event type alone."""
    templates = {
        "blocker": "This appears to be a blocking issue that may affect team velocity.",
        "risk": "This risk may affect project timelines if it escalates.",
        "decision": "The outcome of this decision likely affects multiple workstreams.",
        "request_for_input": "A response may be needed to unblock parallel work.",
        "status_update": "This update may affect scheduling for dependent work.",
    }
    return templates.get(event_type, "")


def _hedge(statement: str) -> str:
    """
    Ensure the statement uses appropriately hedged language.
    If it already contains a hedge word, return as-is.
    """
    if not statement:
        return ""
    hedge_words = ("may", "likely", "appears", "suggests", "could", "typically", "affects")
    lower = statement.lower()
    if any(w in lower for w in hedge_words):
        return statement
    # Prepend a soft hedge
    return "This likely " + statement[0].lower() + statement[1:]
