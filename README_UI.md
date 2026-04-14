# EverCurrent Daily Digest — Demo UI

A Slack-like demo interface for the Daily Digest Engine.

## Prerequisites

- Python 3.10+ with the digest engine installed (`pip install -e .`)
- Node.js 18+ and npm

## Quick Start

### 1. Start the API server

```bash
# From project root
pip install fastapi "uvicorn[standard]"
uvicorn api.server:app --reload --port 8000
```

The server pre-computes all user digests on startup (takes ~5 seconds).
Health check: http://localhost:8000/health

### 2. Start the UI

```bash
cd ui
npm install
npm run dev
```

Opens at http://localhost:3000

## Using the Demo

### View channels
Click any channel in the sidebar (e.g. `#hw-general`) to see its message feed.
Click "N replies · View thread" on any message to inspect the full thread.

### View personalised digests
Click **Digest Bot** at the top of the sidebar, or click any user's name in the DM list.
Each user sees a different digest — ranked for their role and activity patterns.

### Switch users
Use the **user switcher** at the bottom of the sidebar to change which user's digest is shown.
This is the core personalisation demo: Alice (HW), Bob (FW), Carlos (Supply Chain) all see different top items.

### Inspect ranking scores
Click **Debug scores** under any digest item to see the full feature breakdown:
- User affinity, importance, urgency, momentum, novelty, recency, semantic affinity
- Each feature's contribution to the final score

### Trace to source threads
Click **View thread →** on any digest item to open the source Slack thread.

## Architecture

```
Browser (Next.js :3000)
    ↓ HTTP
FastAPI Server (:8000)
    ↓ Python
Digest Engine (src/)
    ↓
Mock Slack Data (data/mock_slack/)
```

## Notes

- The digest uses a fixed date of **2026-04-10** for reproducibility
- No Slack credentials are needed for demo mode
- LLM summarisation is off by default; pass `--llm gemini` to `run_digest.py` for AI summaries
