#!/usr/bin/env python3
"""
Inspect enriched candidate events and their inferred signals.

Usage:
    python scripts/inspect_events.py
    python scripts/inspect_events.py --event evt_m_010
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.ingest import load_workspace
from src.events import build_candidate_events
from src.enrichment import enrich_candidate_events


def main():
    parser = argparse.ArgumentParser(description="Inspect candidate events")
    parser.add_argument("--event", help="Filter to a specific event ID")
    parser.add_argument("--date", default="2026-04-10")
    args = parser.parse_args()

    data_dir = Path(__file__).parent.parent / "data" / "mock_slack"
    now = datetime.fromisoformat(f"{args.date}T12:00:00").replace(tzinfo=timezone.utc)

    workspace = load_workspace(data_dir)
    events = build_candidate_events(workspace)
    enriched = enrich_candidate_events(events, workspace, now=now)

    target = [e for e in enriched if args.event is None or e.event_id == args.event]

    print(f"\nCandidate events ({len(target)} shown)\n")

    for event in target:
        s = event.signals
        print(f"{'=' * 60}")
        print(f"Event:    {event.event_id}")
        print(f"Thread:   {event.thread_id}")
        print(f"Channel:  {event.channel_id}")
        print(f"Messages: {event.message_count}  Replies: {event.reply_count}  Reactions: {event.total_reactions}")
        print(f"Participants ({event.unique_participant_count}): {event.participant_ids}")

        if s:
            print(f"\n  Title:        {s.title}")
            print(f"  Topics:       {s.topic_labels}")
            print(f"  Dominant:     {s.dominant_event_type}  (confidence={s.confidence:.2f})")
            print(f"  Type scores:")
            d = s.event_type_dist
            print(f"    blocker={d.blocker:.2f}  decision={d.decision:.2f}  risk={d.risk:.2f}")
            print(f"    status={d.status_update:.2f}  rfi={d.request_for_input:.2f}  noise={d.noise:.2f}")
            print(f"\n  Signal scores:")
            print(f"    urgency={s.urgency_score:.3f}  momentum={s.momentum_score:.3f}  "
                  f"novelty={s.novelty_score:.3f}")
            print(f"    unresolved={s.unresolved_score:.3f}  importance={s.importance_score:.3f}  "
                  f"cross_func={s.cross_functional_score:.3f}")
        print()


if __name__ == "__main__":
    main()
