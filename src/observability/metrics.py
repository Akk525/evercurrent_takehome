"""
Lightweight pipeline metrics collector.

Designed to be threaded through the pipeline as an optional argument.
If not provided (None), zero overhead — no collection happens.
"""

import time
from dataclasses import dataclass, field
from contextlib import contextmanager
from typing import Generator


@dataclass
class StageTimer:
    """Records wall-clock duration of a named pipeline stage."""

    name: str
    duration_ms: float = 0.0

    @classmethod
    @contextmanager
    def measure(cls, name: str) -> Generator["StageTimer", None, None]:
        """Usage: with StageTimer.measure("enrichment") as t: ..."""
        timer = cls(name=name)
        start = time.perf_counter()
        yield timer
        timer.duration_ms = round((time.perf_counter() - start) * 1000, 1)


@dataclass
class PipelineMetrics:
    """
    Collects metrics across a single pipeline run.

    Pass as an optional argument to pipeline stages.
    If None is passed instead, stages skip collection with no overhead.
    """

    # Pipeline mode: "full" or "online" (pre-computed enrichment)
    pipeline_mode: str = "full"

    # Event counts
    total_candidate_events: int = 0
    events_enriched: int = 0        # Actually ran _enrich_single()
    events_from_cache: int = 0      # Skipped enrichment (clean from processing_state)

    # Embedding
    embedding_cache_hit: bool = False
    embedding_cache_miss: bool = False

    # Per-user ranking
    users_processed: int = 0
    total_candidates_scored: int = 0   # Sum of (events scored) across all users
    total_candidates_pruned: int = 0   # Pruned before scoring, sum across all users

    # Summarization
    summaries_generated: int = 0       # Unique shared summaries built
    summaries_reused: int = 0          # Item-level reuses across all users

    # Stage timers (populated optionally)
    stage_timers: list[StageTimer] = field(default_factory=list)

    def record_stage(self, timer: StageTimer) -> None:
        self.stage_timers.append(timer)

    def summary_dict(self) -> dict:
        """Return a clean summary suitable for printing or JSON output."""
        result: dict = {
            "pipeline_mode": self.pipeline_mode,
            "events": {
                "total": self.total_candidate_events,
                "enriched": self.events_enriched,
                "from_cache": self.events_from_cache,
            },
            "embedding_cache": "hit" if self.embedding_cache_hit else "miss",
            "ranking": {
                "users": self.users_processed,
                "total_scored": self.total_candidates_scored,
                "total_pruned": self.total_candidates_pruned,
            },
            "summarization": {
                "generated": self.summaries_generated,
                "reused": self.summaries_reused,
            },
        }
        if self.stage_timers:
            result["timings_ms"] = {t.name: t.duration_ms for t in self.stage_timers}
        return result

    def print_report(self) -> None:
        """Pretty-print metrics to stdout in a decision-useful format."""
        print("\n" + "=" * 54)
        print("PIPELINE METRICS")
        print("=" * 54)

        mode_label = "full pipeline" if self.pipeline_mode == "full" else "online (pre-enriched)"
        print(f"  Mode             : {mode_label}")

        ev = self.summary_dict()["events"]
        total = ev["total"]
        cached = ev["from_cache"]
        enriched = ev["enriched"]
        if cached > 0:
            print(f"  Candidate events : {total}  (re-enriched {enriched}, skipped {cached} from cache)")
        else:
            print(f"  Candidate events : {total}  (all enriched)")

        emb_cache = "hit — embeddings loaded from disk" if self.embedding_cache_hit else "miss — embeddings computed fresh"
        print(f"  Embedding cache  : {emb_cache}")

        users = self.users_processed
        print(f"  Users processed  : {users}")

        scored = self.total_candidates_scored
        pruned = self.total_candidates_pruned
        if users > 0 and total > 0:
            scored_per_user = scored // users if users else scored
            if pruned > 0:
                pruned_per_user = pruned // users
                print(
                    f"  Ranking          : scored {scored_per_user} of {total} candidates/user "
                    f"(pruned {pruned_per_user} before scoring)"
                )
            else:
                print(f"  Ranking          : scored {scored_per_user} candidates/user (no pruning)")

        gen = self.summaries_generated
        reused = self.summaries_reused
        if gen > 0:
            # reused = total item slots filled from shared pool across all users
            # naive upper bound = gen * users (if every user got every event)
            naive_max = gen * users if users > 0 else gen
            saved = max(0, reused - gen)
            print(
                f"  Summaries        : {gen} generated once, reused {reused}× across "
                f"{users} users (saved ~{saved} redundant calls)"
            )

        timers = self.summary_dict().get("timings_ms", {})
        if timers:
            total_ms = sum(timers.values())
            print()
            print("  Stage timings:")
            for stage, ms in timers.items():
                pct = (ms / total_ms * 100) if total_ms > 0 else 0
                print(f"    {stage:<24} {ms:>7.1f} ms  ({pct:.0f}%)")
        print()
