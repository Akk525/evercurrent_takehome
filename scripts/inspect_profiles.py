#!/usr/bin/env python3
"""
Inspect inferred user context profiles.

Usage:
    python scripts/inspect_profiles.py
    python scripts/inspect_profiles.py --user u_alice
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
from src.profiles import build_user_profiles


def main():
    parser = argparse.ArgumentParser(description="Inspect user profiles")
    parser.add_argument("--user", help="Filter to a specific user ID")
    parser.add_argument("--date", default="2026-04-10")
    args = parser.parse_args()

    data_dir = Path(__file__).parent.parent / "data" / "mock_slack"
    now = datetime.fromisoformat(f"{args.date}T12:00:00").replace(tzinfo=timezone.utc)

    workspace = load_workspace(data_dir)
    events = build_candidate_events(workspace)
    enriched = enrich_candidate_events(events, workspace, now=now)
    profiles = build_user_profiles(workspace, enriched)

    user_by_id = {u.user_id: u for u in workspace.users}
    target = {
        uid: p for uid, p in profiles.items()
        if args.user is None or uid == args.user
    }

    print(f"\nUser profiles ({len(target)} shown)\n")

    for uid, profile in target.items():
        user = user_by_id.get(uid)
        display_name = user.display_name if user else uid
        role = user.role if user else "unknown"

        print(f"{'=' * 60}")
        print(f"User:     {uid} — {display_name} ({role})")
        print(f"Activity: {profile.activity_level:.2f} (normalised)")
        print(f"Channels: {profile.active_channel_ids}")
        print(f"Recent threads: {profile.recent_thread_ids}")

        if profile.topic_affinities:
            top_topics = sorted(
                profile.topic_affinities.items(), key=lambda x: x[1], reverse=True
            )[:5]
            print(f"Top topics: {[(t, f'{s:.2f}') for t, s in top_topics]}")

        if profile.event_type_affinities:
            top_types = sorted(
                profile.event_type_affinities.items(), key=lambda x: x[1], reverse=True
            )[:3]
            print(f"Event type affinities: {[(t, f'{s:.2f}') for t, s in top_types]}")

        print(f"Collaborators: {profile.frequent_collaborators}")
        print()


if __name__ == "__main__":
    main()
