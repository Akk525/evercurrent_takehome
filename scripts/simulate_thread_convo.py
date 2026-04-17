"""
Simulate Alice and Bob's conversation as replies on a channel thread,
so it gets picked up by the digest engine.

Default target: m_030 in #hw-general ("Decision needed: Rev C vs Rev B")
— directly relevant to Alice's board validation and Bob's firmware work.

Usage:
    python scripts/simulate_thread_convo.py
    python scripts/simulate_thread_convo.py --thread m_010   # BMS firmware thread
    python scripts/simulate_thread_convo.py --delay 2.0
    python scripts/simulate_thread_convo.py --dry-run
"""

import argparse
import time
import urllib.request
import urllib.parse
import json

API_URL = "http://localhost:8000"

# Thread options — pick whichever fits the demo best
THREADS = {
    "m_030": "#hw-general — Decision: Rev C vs Rev B board",
    "m_010": "#firmware  — BMS bring-up hang",
    "m_001": "#suppliers — MX150 connector delay",
}

# The conversation — realistic back-and-forth between Alice (HW) and Bob (FW)
REPLIES = [
    ("u_alice", "Flagging this here — Carlos just confirmed the oscillator parts are delayed by at least 10 days. That directly impacts our Rev C validation timeline."),
    ("u_bob",   "Does that push the full RF test suite? I was counting on those results before finalising the firmware sleep modes."),
    ("u_alice", "RF suite is blocked yes, but I can still run power rail validation in parallel. Should have numbers for you by EOD."),
    ("u_bob",   "That works. I need the power numbers to tune the PMIC sequencing anyway. Can you share the bench data directly when it's ready?"),
    ("u_alice", "Will do. Also — separate issue but related — the USB-C PD negotiation failure we flagged last week traced back to a footprint error on R47. We're respinning that section."),
    ("u_bob",   "Ouch. Does that affect the integration build schedule?"),
    ("u_alice", "Pushing by ~2 days. I'll post a proper update in #hw-integration once I confirm with the fab."),
    ("u_bob",   "Understood. I'll hold the USB firmware stack merge until you confirm. No point landing it if the hardware side is changing."),
]

NAMES = {
    "u_alice": "Alice",
    "u_bob":   "Bob",
}


def post_reply(thread_id: str, sender: str, text: str) -> dict:
    url = f"{API_URL}/api/threads/{thread_id}/reply?as={urllib.parse.quote(sender)}"
    data = json.dumps({"text": text}).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def main():
    parser = argparse.ArgumentParser(description="Simulate Alice ↔ Bob replies on a channel thread")
    parser.add_argument("--thread", default="m_030", choices=list(THREADS.keys()),
                        help="Thread to post replies on (default: m_030)")
    parser.add_argument("--delay", type=float, default=1.0,
                        help="Seconds between replies (default: 1.0)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print replies without sending")
    args = parser.parse_args()

    print(f"Target thread: {args.thread} — {THREADS[args.thread]}")
    print(f"Posting {len(REPLIES)} replies")
    if args.dry_run:
        print("(dry run — not sending)\n")
    else:
        print(f"(delay: {args.delay}s between replies)\n")

    for i, (sender, text) in enumerate(REPLIES, 1):
        print(f"[{i}/{len(REPLIES)}] {NAMES[sender]}: {text}")

        if not args.dry_run:
            try:
                post_reply(args.thread, sender, text)
            except Exception as e:
                print(f"  ERROR: {e}")
                print("  Is the API server running? uvicorn api.server:app --reload --port 8000")
                return

            if i < len(REPLIES):
                time.sleep(args.delay)

    if not args.dry_run:
        print("\nDone. Digest will refresh in ~15 seconds.")
        print(f"Open the thread in the UI: #hw-general → find the Rev C/Rev B decision thread")
        print("Then check Digest Bot — this thread should rise in Alice and Bob's digests.")


if __name__ == "__main__":
    main()
