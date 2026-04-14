#!/usr/bin/env python3
"""
Run the digest engine and output per-user digests.

Usage:
    python scripts/run_digest.py                                   # All users
    python scripts/run_digest.py --user u_alice                    # One user
    python scripts/run_digest.py --user u_alice --llm gemini       # With LLM
    python scripts/run_digest.py --output outputs/                 # Save JSON
    python scripts/run_digest.py --metrics                         # Show perf report
    python scripts/run_digest.py --from-enrichment outputs/enrichment.json  # Online mode
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow running from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.digest import run_full_pipeline, run_online_digest
from src.summarization.providers import FallbackProvider, GeminiProvider


def main():
    parser = argparse.ArgumentParser(description="Run the daily digest engine")
    parser.add_argument("--user", help="Run for a single user ID only")
    parser.add_argument("--top-k", type=int, default=5, help="Max items per digest")
    parser.add_argument(
        "--llm",
        choices=["gemini", "none"],
        default="none",
        help="LLM provider (default: none = fallback mode)",
    )
    parser.add_argument(
        "--output",
        help="Directory to save digest JSON files",
    )
    parser.add_argument(
        "--date",
        default="2026-04-10",
        help="Digest date (YYYY-MM-DD, default: 2026-04-10)",
    )
    parser.add_argument(
        "--debug-user",
        metavar="USER_ID",
        help="Show full ranking breakdown for a user, including excluded events",
    )
    parser.add_argument(
        "--from-enrichment",
        metavar="PATH",
        help="Path to a pre-computed enrichment snapshot (skips enrichment stage)",
    )
    parser.add_argument(
        "--metrics",
        action="store_true",
        help="Print pipeline performance metrics after the run",
    )
    args = parser.parse_args()

    data_dir = Path(__file__).parent.parent / "data" / "mock_slack"

    # Select LLM provider
    if args.llm == "gemini":
        try:
            provider = GeminiProvider()
            print("[llm] Using Google Gemini for summarization")
        except Exception as e:
            print(f"[warn] Could not init Gemini provider: {e}. Falling back.")
            provider = FallbackProvider()
    else:
        provider = FallbackProvider()
        print("[llm] Using fallback (rule-based) summarization")

    user_ids = [args.user] if args.user else None
    debug_user = args.debug_user

    # Fix the clock so recency scores are deterministic
    now = datetime.fromisoformat(f"{args.date}T12:00:00").replace(tzinfo=timezone.utc)

    # If debug mode: force include_excluded=True for the target user
    include_excluded = debug_user is not None
    if debug_user and not user_ids:
        user_ids = [debug_user]

    # Set up metrics collector if requested
    metrics = None
    if args.metrics:
        from src.observability import PipelineMetrics
        metrics = PipelineMetrics()

    if args.from_enrichment:
        enrichment_path = Path(args.from_enrichment)
        print(f"\nRunning online digest (from enrichment snapshot: {enrichment_path})")
        print(f"Date: {args.date}\n")
        digests = run_online_digest(
            enrichment_path=enrichment_path,
            user_ids=user_ids,
            top_k=args.top_k,
            provider=provider,
            now=now,
            date_str=args.date,
            include_excluded=include_excluded,
        )
    else:
        print(f"\nRunning digest pipeline for date: {args.date}")
        print(f"Data dir: {data_dir}\n")
        digests = run_full_pipeline(
            data_dir=data_dir,
            user_ids=user_ids,
            top_k=args.top_k,
            provider=provider,
            now=now,
            date_str=args.date,
            include_excluded=include_excluded,
            metrics=metrics,
        )

    for uid, digest in digests.items():
        print_digest(digest)

    if debug_user and debug_user in digests:
        print_debug(digests[debug_user])

    if metrics is not None:
        metrics.print_report()

    if args.output:
        output_dir = Path(args.output)
        output_dir.mkdir(parents=True, exist_ok=True)
        for uid, digest in digests.items():
            out_path = output_dir / f"digest_{uid}_{args.date}.json"
            with open(out_path, "w") as f:
                json.dump(digest.model_dump(mode="json"), f, indent=2, default=str)
            print(f"  Saved: {out_path}")


def print_digest(digest):
    print("=" * 70)
    print(f"DIGEST — {digest.user_id}  [{digest.date}]")
    print(f"  {digest.headline}")
    print(f"  Candidates considered: {digest.total_candidates_considered}")
    print(f"  LLM used: {digest.llm_used}")
    print()

    for i, item in enumerate(digest.items, 1):
        print(f"  [{i}] {item.title}")
        print(f"       Score: {item.score:.3f}  |  Type: {item.event_type}  |  Signal: {item.signal_level}")
        print(f"       Confidence: {item.confidence:.2f}")
        if item.summary:
            print(f"       Summary: {item.summary}")
        if item.why_shown:
            print(f"       Why: {item.why_shown}")
        f = item.reason_features
        print(
            f"       Features: affinity={f.user_affinity:.2f} importance={f.importance:.2f} "
            f"urgency={f.urgency:.2f} momentum={f.momentum:.2f} "
            f"novelty={f.novelty:.2f} recency={f.recency:.2f} "
            f"emb_affinity={f.embedding_affinity:.2f}"
        )
        thread_ids = ", ".join(item.source_thread_ids)
        msg_count = len(item.source_message_ids)
        print(f"       Source: thread(s) [{thread_ids}]  |  {msg_count} message(s)")
        print()
    print()


def print_debug(digest):
    """Print the full ranking breakdown including events that didn't make the cut."""
    print("=" * 70)
    print(f"DEBUG VIEW — {digest.user_id}  (events excluded from digest)")
    print()

    if not digest.excluded_items:
        print("  No excluded items (all events made the cut or include_excluded not set)")
        print()
        return

    for item in digest.excluded_items:
        print(f"  [EXCLUDED] {item.title}")
        print(f"             Score: {item.score:.3f}")
        print(f"             Reason: {item.top_exclusion_reason}")
        print()
    print()


if __name__ == "__main__":
    main()
