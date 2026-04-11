# Daily Digest Engine — Hardware Engineering Teams

A local, runnable **digest engine** that ingests mock Slack data, infers contextual signal from it, and generates a personalised daily digest per engineer.

---

## Problem

Hardware teams communicate heavily in Slack. Important updates — blockers, supply-chain risks, board bring-up failures — sit buried in threads alongside social chatter. Different engineers care about different things. There is no reliable way to surface what actually matters to each person without reading every thread.

This engine automates that inference, using only Slack activity as input.

---

## Why Slack-only inference?

A simpler approach might pull structured metadata from Jira, Notion, or org charts. We deliberately avoid this for two reasons:

1. **Integration brittleness.** Real organisations don't have clean, up-to-date structured metadata. Jira tickets get stale. Notion pages go unlinked. Slack is where work actually happens.
2. **Ground-truth overconfidence.** Structured tools impose categories that may not match reality. Inferring from communication preserves uncertainty honestly.

All claims in digest outputs use probabilistic language ("appears", "likely", "suggests") to reflect this.

---

## Architecture

```
Raw Slack Data
    ↓
[1] Ingestion          — Load JSON → typed Pydantic models
    ↓
[2] Event Construction — 1 thread = 1 CandidateEvent
    ↓
[3] Semantic Enrichment — Heuristic signal computation:
                          urgency, importance, momentum, novelty, unresolved,
                          topic labels, event type distribution
    ↓
[4] User Profiling     — Behavioural profiles from participation history:
                          topic affinities, channel activity, collaborators
    ↓
[5] Per-user Ranking   — Weighted scoring: affinity + importance + urgency
                          + momentum + novelty + recency
                          (fully explainable, no black box)
    ↓
[6] LLM Summarization  — Applied ONLY to top-k selected items
                          (fallback mode requires no LLM access)
    ↓
[7] Digest Assembly    — Final per-user JSON output
```

**Critical design rule:** ranking happens before LLM calls. The LLM never participates in selection — only in summarising what was already selected.

---

## Data Model

### Raw entities
| Entity | Key fields |
|--------|------------|
| `SlackMessage` | message_id, thread_id, channel_id, user_id, text, timestamp, mentions, reaction_counts |
| `SlackThread` | thread_id, channel_id, participant_ids, message_ids, started_at, last_activity_at |
| `SlackUser` | user_id, display_name, role (optional), channel_ids |
| `SlackChannel` | channel_id, name, topic, member_ids |

### Derived entities
| Entity | Purpose |
|--------|---------|
| `CandidateEvent` | One thread → one ranked unit, with aggregated stats |
| `SemanticSignals` | Inferred signals: urgency, importance, momentum, novelty, unresolved, topic labels, event type distribution |
| `UserContextProfile` | Behavioural profile: topic affinities, channel activity, collaborators |
| `RankingFeatures` | Per-feature scores + weights used + final score |
| `RankedDigestItem` | Digest item with full traceability to source threads/messages |
| `DailyDigest` | Final per-user payload |

---

## Ranking Logic

Score for a (user, event) pair:

```
score = 0.30 × user_affinity
      + 0.25 × importance
      + 0.20 × urgency
      + 0.10 × momentum
      + 0.10 × novelty
      + 0.05 × recency
```

**user_affinity** combines: direct thread participation (+0.4), channel affinity, topic overlap with user profile, collaborator overlap.

**importance** combines: blocker signal, risk signal, decision signal, participant count, reaction count.

**urgency** is derived from: deadline/urgency keyword density, alert-type reaction counts.

**momentum** is derived from: messages-per-hour rate, participant diversity.

**novelty** is derived from: topic overlap with other events in same channel — high overlap = lower novelty.

**recency** decays exponentially with half-life of 12 hours.

Weights are defined in `src/ranking/ranker.py:DEFAULT_WEIGHTS` and are trivially tunable without touching scoring logic.

---

## Mock Dataset

Located in `data/mock_slack/`. Simulates a hardware engineering team's Slack workspace:

- **4 channels**: hw-general, suppliers-and-procurement, firmware-bringup, test-and-validation
- **8 users**: 3 hardware engineers, 2 firmware engineers, 1 test engineer, 1 supply chain manager, 1 program manager
- **7 threads** designed to exercise all system behaviours:

| Thread | Type | What it tests |
|--------|------|---------------|
| MX150 connector delay | Risk/supply-chain | Urgency, mentions, supplier keywords |
| I2C hang on BMS bringup | Blocker | High-signal blocker, firmware-specific |
| Thermal cycling failures | Risk | Qual blocker, cross-functional participants |
| Rev C vs Rev B decision | Decision | Multi-participant decision, linked to other threads |
| Team lunch | Noise | Social thread — should score low |
| ADC read failures | Blocker | Rising topic, I2C pattern echoes the BMS thread |
| NOR flash allocation | Risk | Secondary supply-chain risk, lower urgency |

---

## Project Structure

```
digest_engine/
├── pyproject.toml
├── README.md
├── src/
│   ├── models/
│   │   ├── raw.py        — SlackMessage, SlackThread, SlackUser, SlackChannel
│   │   └── derived.py    — CandidateEvent, SemanticSignals, UserContextProfile,
│   │                        RankingFeatures, RankedDigestItem, DailyDigest
│   ├── ingest/
│   │   └── loader.py     — JSON → typed model deserialization
│   ├── events/
│   │   └── builder.py    — Thread → CandidateEvent construction
│   ├── enrichment/
│   │   ├── keywords.py   — Domain keyword lists (tunable)
│   │   ├── signals.py    — Individual signal computation functions
│   │   └── enricher.py   — Orchestrates enrichment per event
│   ├── profiles/
│   │   └── profiler.py   — Behavioural user profile inference
│   ├── ranking/
│   │   └── ranker.py     — Weighted scoring, user affinity, top-k selection
│   ├── summarization/
│   │   ├── providers.py  — LLMProvider ABC, FallbackProvider, GeminiProvider
│   │   └── summarizer.py — Runs summarization on post-ranked items only
│   └── digest/
│       └── assembler.py  — Wires full pipeline, produces DailyDigest
├── data/mock_slack/
│   ├── users.json
│   ├── channels.json
│   ├── messages.json
│   └── threads.json
├── scripts/
│   ├── run_digest.py       — Run digest for one or all users
│   ├── inspect_events.py   — Inspect enriched candidate events
│   └── inspect_profiles.py — Inspect inferred user profiles
├── tests/
│   ├── conftest.py
│   └── test_ranking_behaviour.py
└── outputs/               — Generated digest JSONs (gitignored)
```

---

## How to Run

### Install

```bash
pip install -e ".[dev]"
```

### Build digests for all users

```bash
python scripts/run_digest.py --date 2026-04-10
```

### Build digest for one user

```bash
python scripts/run_digest.py --user u_alice
python scripts/run_digest.py --user u_bob
python scripts/run_digest.py --user u_fiona
```

### Save digests to JSON files

```bash
python scripts/run_digest.py --output outputs/ --date 2026-04-10
```

### Inspect enriched candidate events

```bash
python scripts/inspect_events.py
python scripts/inspect_events.py --event evt_m_010
```

### Inspect inferred user profiles

```bash
python scripts/inspect_profiles.py
python scripts/inspect_profiles.py --user u_alice
```

### Run tests

```bash
pytest tests/ -v
```

### Use LLM summarization (optional)

```bash
export GEMINI_API_KEY=...
python scripts/run_digest.py --llm gemini
```

---

## LLM Integration

`FallbackProvider` is used by default — no API key needed. It generates rule-based summaries and "why shown" text.

`GeminiProvider` wraps Google Gemini via the official `google-genai` SDK. It only gets called on the top-k ranked items after selection is complete. The LLM does not influence ranking in any way.

To add a new provider: implement `LLMProvider.summarize(event, item, profile) -> (summary, why_shown)` in `src/summarization/providers.py`.

---

## Limitations

- **Heuristic enrichment is noisy.** Keyword-based signal detection will misclassify unusual threads. A model-based classification layer would be more robust.
- **1 thread = 1 event is an MVP simplification.** Related threads across channels (e.g. the I2C issue appearing in both firmware and testing) are not clustered into a single event.
- **Novelty is computed at channel scope only.** Cross-channel novelty is not computed.
- **User profiles are cold-start limited.** A user with few messages has sparse profile data and will receive lower-relevance digests.
- **No time window filtering.** All threads in the dataset are considered regardless of age. A production system would apply a rolling window.
- **No feedback loop.** Click/read signals would substantially improve affinity scoring over time.

---

## Potential Improvements

- **Thread clustering**: group related threads across channels into a single candidate event (e.g. using embedding similarity)
- **Embedding-based topic modeling**: replace keyword heuristics with a small embedding model for more robust topic inference
- **Feedback-driven weight tuning**: learn per-user weights from digest interaction signals
- **Time-window filtering**: only ingest threads active within the last 24–48 hours
- **Incremental profiles**: maintain running profiles rather than rebuilding from scratch each run
- **Real Slack API ingestion**: the data model is already compatible — just replace `loader.py`
