#!/usr/bin/env python3
"""
Offline enrichment script — run expensive enrichment once, cache to disk.

Usage:
    python scripts/run_enrich.py                          # Default output path
    python scripts/run_enrich.py --output outputs/enrichment.json
    python scripts/run_enrich.py --date 2026-04-10

The output file can then be passed to run_digest.py --from-enrichment.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.digest import run_offline_enrichment


def main():
    parser = argparse.ArgumentParser(description="Run offline enrichment and cache to disk")
    parser.add_argument(
        "--output",
        default="outputs/enrichment.json",
        help="Path to write enrichment snapshot (default: outputs/enrichment.json)",
    )
    parser.add_argument(
        "--date",
        default="2026-04-10",
        help="Reference date for enrichment (YYYY-MM-DD, default: 2026-04-10)",
    )
    args = parser.parse_args()

    data_dir = Path(__file__).parent.parent / "data" / "mock_slack"
    output_path = Path(args.output)

    now = datetime.fromisoformat(f"{args.date}T12:00:00").replace(tzinfo=timezone.utc)

    print(f"Running offline enrichment for date: {args.date}")
    print(f"Data dir:    {data_dir}")
    print(f"Output path: {output_path}\n")

    payload = run_offline_enrichment(data_dir, output_path, now=now)

    event_count = len(payload.get("enriched_events", []))
    profile_count = len(payload.get("profiles", {}))
    print(f"\nDone. {event_count} events enriched, {profile_count} user profiles built.")
    print(f"Snapshot written to: {output_path}")
    print(f"\nNext: python scripts/run_digest.py --from-enrichment {output_path}")


if __name__ == "__main__":
    main()
