# Slack Integration

This document covers the optional Slack integration layer added to the Daily Digest Engine. The core engine works entirely on local mock data without Slack credentials; the integration is an additive layer that provides real-data ingestion and digest delivery.

---

## Architecture

```
Slack workspace
     │
     │  Events API / Socket Mode (pushed events)
     ▼
┌─────────────────────┐
│  Ingestion layer     │  src/slack_ingest/
│  - validate events   │  events.py, http_events.py, socket_mode.py
│  - deduplicate       │
│  - store locally     │
│  - mark threads dirty│
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│  Local event store   │  src/slack_ingest/store.py
│  (SQLite)            │  data/slack_ingest.db
│  - messages          │
│  - threads           │
│  - channels / users  │
│  - dirty markers     │
└──────────┬──────────┘
           │                  ┌──────────────────────┐
           │◄─────────────────│  Reconciliation worker│
           │  targeted reads  │  src/slack_ingest/    │
           │  (dirty threads) │  reconciler.py        │
           │                  └──────────────────────┘
           │
           ▼
┌─────────────────────┐
│  Digest engine       │  unchanged — reasons over local data
│  (pipeline)          │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│  Delivery layer      │  src/slack_delivery/
│  - Block Kit blocks  │  block_kit.py, sender.py
│  - DM via Web API    │
│  - or webhook        │
└─────────────────────┘
```

---

## Why event-driven ingestion instead of polling

Slack Tier-1 methods (`conversations.history`, `conversations.replies`) are the most heavily rate-limited endpoints in the API — as low as 1 RPM for certain app types. A naive approach that polls every channel every minute will hit rate limits within seconds on any real workspace.

The event-driven approach avoids this by:

1. **Receiving pushed events** — Slack delivers new messages to your endpoint in real time. No read API calls needed for normal operation.
2. **Marking only dirty threads** — when a reply event arrives, the thread is marked as needing reconciliation. Reconciliation fetches that one thread's replies on demand, not all threads.
3. **Using history only for backfill** — `conversations.history` is called once per channel during initial setup, not repeatedly.

This means steady-state operation uses zero Tier-1 API calls. Rate-limited endpoints are reserved for backfill and repair.

---

## How reconciliation works

`ReconciliationWorker` (`src/slack_ingest/reconciler.py`) runs as an async background loop:

1. Wakes every `poll_interval` seconds (default: 60s)
2. Fetches up to `max_threads_per_run` dirty threads from the local store
3. For each dirty thread:
   - Acquires a Tier-1 token from the rate limiter (blocks if token unavailable)
   - Calls `conversations.replies` for that thread
   - Stores new messages, marks thread clean
4. Handles 429 responses by applying `Retry-After` to the token bucket

Dirty threads are the bridge between event processing and reconciliation:
- **Event received** → `_handle_new_message` marks thread dirty if it has replies
- **Reconciler runs** → fetches only dirty threads → marks them clean

This means reconciliation is always targeted, never blanket.

---

## Rate-limit handling

`src/slack_ingest/rate_limits.py` implements a token bucket per Slack API method:

| Method | RPM cap |
|---|---|
| `conversations.history` | 1.0 |
| `conversations.replies` | 1.0 |
| `chat.postMessage` | 40.0 |
| `conversations.list` | 10.0 |
| `users.info` | 20.0 |
| `auth.test` | 100.0 |

Buckets hold 1 token, refilling at `rpm/60` tokens per second. When a 429 response arrives:
1. `handle_429(method, retry_after)` sets a future unblock time on the bucket
2. `try_acquire` returns `False` until the block expires
3. `parse_retry_after(headers)` parses the `Retry-After` header (minimum 1 second, default 60 seconds if absent/invalid)

`RateLimiter.metrics()` exposes hit counts, blocked methods, and `app_rate_limited` event counts for observability.

---

## How to enable Slack integration

### Required credentials

```bash
export SLACK_BOT_TOKEN=xoxb-...          # Bot OAuth token
export SLACK_SIGNING_SECRET=...          # From Slack app settings (for HTTP Events API)
# OR for Socket Mode:
export SLACK_APP_TOKEN=xapp-...          # App-level token with connections:write scope
```

### Optional settings

```bash
export SLACK_USER_MAP='{"u_alice":"U012AB3CD","u_bob":"U034EF5GH"}'
# OR place config/slack_user_map.json (copy from config/slack_user_map.example.json)

export SLACK_WEBHOOK_URL=https://hooks.slack.com/...  # Alternative delivery path
export SLACK_DRY_RUN=1                                # Print payloads, don't send
```

---

## Run modes

### Local mode (no Slack credentials needed)

```bash
# API server
uvicorn api.server:app --reload --port 8000

# CLI digest generation
python scripts/run_digest.py

# With Gemini LLM summarization
GEMINI_API_KEY=... python scripts/run_digest.py --llm gemini
```

### Slack-integrated mode

```bash
# Start the API server — the Slack Events router mounts automatically
SLACK_BOT_TOKEN=xoxb-... \
SLACK_SIGNING_SECRET=... \
uvicorn api.server:app --reload --port 8000

# The Events API endpoint is available at:
# POST http://localhost:8000/slack/events
# GET  http://localhost:8000/slack/metrics
# POST http://localhost:8000/slack/reconcile/{channel_id}

# For Socket Mode (no public URL needed):
SLACK_APP_TOKEN=xapp-... python -m src.slack_ingest.socket_mode
```

### Dry-run delivery mode

```bash
# Prints Block Kit JSON payload, makes no Slack API calls
SLACK_BOT_TOKEN=xoxb-... \
SLACK_DRY_RUN=1 \
SLACK_USER_MAP='{"u_alice":"U012AB3CD"}' \
python scripts/run_digest.py
```

---

## User mapping

Engine user IDs (e.g. `u_alice`) come from the mock dataset. Real Slack member IDs (e.g. `U012AB3CD`) come from your workspace.

Three configuration options, in priority order:

1. **`SLACK_USER_MAP` env var** — JSON string mapping engine IDs to Slack IDs
2. **`config/slack_user_map.json`** — file-based mapping (copy from `config/slack_user_map.example.json`)
3. **Empty map** — delivery skips unmapped users and logs a warning

When a user ID is not mapped, delivery returns `False` for that user and logs:
```
No Slack user ID mapped for engine user 'u_alice'. Add it to SLACK_USER_MAP. Skipping.
```

---

## What local-only mode still supports

With no Slack credentials:
- Full pipeline: ingest mock data → enrich → rank → assemble digest
- Issue memory (SQLite at `data/issue_memory.db`)
- Impact reasoning
- Embedding-based issue linking
- LLM summarization (Gemini if `GEMINI_API_KEY` set, fallback otherwise)
- Block Kit payload generation (`SLACK_DRY_RUN=1`)
- Demo UI (`ui/` directory)

---

## What is not production-grade

**Acknowledged limitations of the current implementation:**

1. **No workspace bootstrap** — the `ReconciliationWorker.backfill_channel()` method is implemented but not wired to automatic channel discovery at startup. You must call `POST /slack/reconcile/{channel_id}` manually or implement channel listing.

2. **No reconnect logic for Socket Mode** — `SocketModeManager` wraps the SDK client but does not implement exponential backoff reconnection. The SDK itself handles some reconnection; production systems should add explicit retry.

3. **SQLite is single-writer** — `SlackIngestStore` uses one connection per call. Fine for a single-process deployment; not suitable for multi-process setups without WAL mode or a proper database.

4. **User identity is manual** — there is no automatic Slack user lookup to build the engine-to-Slack ID mapping. You must populate it manually.

5. **Event delivery is not guaranteed** — if the server is down when Slack sends an event, that event is lost. A production system would need event buffering or a queue.

6. **No message threading for Socket Mode ACK** — the `SocketModeManager` ACKs all events before processing. If processing fails, the event is still acknowledged (at-most-once semantics).

7. **`api/slack_events.py` requires a public HTTPS URL** — for the HTTP Events API path. Socket Mode avoids this for local dev, but the HTTP path is better for production.

8. **Rate limit state is in-memory** — `RateLimiter` state resets on restart. A restarted server does not know about active Retry-After windows from the previous run.
