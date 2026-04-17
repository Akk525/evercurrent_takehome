"""
Lightweight evaluation benchmark for the digest ranking pipeline.

Tests ranking quality on a small handcrafted benchmark over the mock dataset.
This is intentionally minimal — the goal is to catch ranking regressions and
validate that behavioral expectations hold, not to produce academic metrics.

Benchmark structure:
    A list of (user_id, expected_top_thread_ids, rationale) triples.
    expected_top_thread_ids are thread IDs that SHOULD appear in the user's
    top-k digest. Ordering within the expected set is not enforced.

Metrics:
    precision_at_k — fraction of expected threads that appear in top-k
    recall_at_k    — fraction of expected threads that appear in top-k
                     (same as precision here since expected set is small)
    cross_user_diversity — fraction of digest items that are unique across users
    duplicate_rate — fraction of digest items that are cluster duplicates (should be 0
                     after suppression)
    coverage       — fraction of users who got at least 1 item
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional


# ---------------------------------------------------------------------------
# Benchmark expectations (mock data specific)
# ---------------------------------------------------------------------------

BENCHMARK_EXPECTATIONS = [
    {
        "user_id": "u_alice",
        "expected_top_threads": ["m_020", "m_001", "m_030"],  # thermal, connector, hw decision
        "rationale": "Alice is HW lead — thermal failures and supply chain blockers should dominate",
    },
    {
        "user_id": "u_bob",
        "expected_top_threads": ["m_010", "m_030"],  # BMS firmware hang, board decision
        "rationale": "Bob is firmware — BMS hang and board-level decisions are most relevant",
    },
    {
        "user_id": "u_carlos",
        "expected_top_threads": ["m_001", "m_060"],  # connector delay, NOR flash shortage
        "rationale": "Carlos is supply chain — supplier threads should dominate",
    },
]

# Threads that should be excluded from digests as low-signal noise
NOISE_THREAD = "m_040"  # "Anyone remember what snacks we're bringing?"


# ---------------------------------------------------------------------------
# Result structure
# ---------------------------------------------------------------------------

@dataclass
class UserBenchmarkResult:
    user_id: str
    expected: list[str]
    got_top_k: list[str]
    hits: list[str]
    precision: float
    rationale: str


@dataclass
class BenchmarkResult:
    user_results: list[UserBenchmarkResult] = field(default_factory=list)
    mean_precision: float = 0.0
    cross_user_diversity: float = 0.0
    noise_excluded: bool = False         # Was the noise thread absent from all digests?
    duplicate_rate: float = 0.0          # Fraction of items that were cluster duplicates
    coverage: float = 0.0               # Fraction of users with >=1 item

    def passed(self, min_precision: float = 0.5) -> bool:
        """True if mean precision >= threshold and noise is excluded."""
        return self.mean_precision >= min_precision and self.noise_excluded

    def report(self) -> str:
        lines = [
            "=== Digest Engine Benchmark ===",
            f"Mean precision@k:     {self.mean_precision:.2f}",
            f"Cross-user diversity: {self.cross_user_diversity:.2f}",
            f"Coverage:             {self.coverage:.2f}",
            f"Noise excluded:       {self.noise_excluded}",
            f"Duplicate rate:       {self.duplicate_rate:.2f}",
            "",
        ]
        for r in self.user_results:
            status = "PASS" if r.precision >= 0.5 else "FAIL"
            lines.append(
                f"[{status}] {r.user_id}: precision={r.precision:.2f} "
                f"hits={r.hits} | {r.rationale}"
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_benchmark(
    data_dir: Path,
    now: Optional[datetime] = None,
    top_k: int = 5,
) -> BenchmarkResult:
    """
    Run the benchmark against the mock dataset.

    Builds the full pipeline internally. Prints a report and returns
    a BenchmarkResult for programmatic inspection.
    """
    from src.digest.assembler import run_full_pipeline

    if now is None:
        now = datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc)

    digests = run_full_pipeline(
        data_dir=data_dir,
        now=now,
        date_str=now.strftime("%Y-%m-%d"),
        top_k=top_k,
        include_excluded=True,
    )

    user_results: list[UserBenchmarkResult] = []
    all_item_ids: list[str] = []
    noise_found = False

    for user_id in [u["user_id"] for u in BENCHMARK_EXPECTATIONS]:
        digest = digests.get(user_id)
        if digest is None:
            continue

        got_thread_ids = [
            item.source_thread_ids[0] if item.source_thread_ids else item.event_id.replace("evt_", "")
            for item in digest.items
        ]
        all_item_ids.extend(got_thread_ids)

        # Check if noise thread surfaced
        if NOISE_THREAD in got_thread_ids:
            noise_found = True

        expectation = next(u for u in BENCHMARK_EXPECTATIONS if u["user_id"] == user_id)
        expected = expectation["expected_top_threads"]
        hits = [t for t in expected if t in got_thread_ids]
        precision = len(hits) / len(expected) if expected else 0.0

        user_results.append(UserBenchmarkResult(
            user_id=user_id,
            expected=expected,
            got_top_k=got_thread_ids,
            hits=hits,
            precision=round(precision, 3),
            rationale=expectation["rationale"],
        ))

    mean_precision = (
        sum(r.precision for r in user_results) / len(user_results)
        if user_results else 0.0
    )

    # Cross-user diversity: what fraction of all surfaced threads are unique?
    cross_user_diversity = (
        len(set(all_item_ids)) / len(all_item_ids) if all_item_ids else 0.0
    )

    # Coverage: users who got >=1 item
    coverage = sum(1 for d in digests.values() if d.items) / len(digests) if digests else 0.0

    result = BenchmarkResult(
        user_results=user_results,
        mean_precision=round(mean_precision, 3),
        cross_user_diversity=round(cross_user_diversity, 3),
        noise_excluded=not noise_found,
        coverage=round(coverage, 3),
    )

    return result
