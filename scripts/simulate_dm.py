"""
Simulate a DM conversation between Alice and Bob.

Usage:
    python scripts/simulate_dm.py
    python scripts/simulate_dm.py --delay 1.5   # custom delay between messages
    python scripts/simulate_dm.py --dry-run      # print messages without sending
"""

import argparse
import time
import urllib.request
import urllib.parse
import json

API_URL = "http://localhost:8000"

CONVERSATION = [
    ("u_alice", "u_bob", "Hey, did you see the latest on the STM32 bring-up thread? Carlos flagged a potential delay on the oscillators"),
    ("u_bob",   "u_alice", "Yeah just caught that. Honestly not surprised — we've been waiting on those for 3 weeks. Does it block your board validation?"),
    ("u_alice", "u_bob", "It blocks the full RF test suite but I can keep going with the power rail validation in the meantime. Should have results by EOD"),
    ("u_bob",   "u_alice", "Good. I need those power numbers before I can finalise the firmware sleep modes. Can you share the bench data when you're done?"),
    ("u_alice", "u_bob", "Sure. Also heads up — the USB-C PD negotiation issue from last week turned out to be a footprint error on R47. We're respinning that section"),
    ("u_bob",   "u_alice", "Ouch. Does that push the integration build?"),
    ("u_alice", "u_bob", "Probably by 2 days. I'll update the hw-integration channel once I confirm with the fab"),
    ("u_bob",   "u_alice", "Appreciated. I'll hold off on merging the USB firmware stack until you confirm"),
]

NAMES = {
    "u_alice": "Alice",
    "u_bob":   "Bob",
}


def send_dm(sender: str, recipient: str, text: str) -> dict:
    url = f"{API_URL}/api/dm/{recipient}?as={urllib.parse.quote(sender)}"
    data = json.dumps({"text": text}).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def main():
    parser = argparse.ArgumentParser(description="Simulate Alice ↔ Bob DM conversation")
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds between messages (default: 1.0)")
    parser.add_argument("--dry-run", action="store_true", help="Print messages without sending")
    args = parser.parse_args()

    print(f"Simulating {len(CONVERSATION)} messages between Alice and Bob")
    if args.dry_run:
        print("(dry run — not sending)\n")
    else:
        print(f"(delay: {args.delay}s between messages)\n")

    for i, (sender, recipient, text) in enumerate(CONVERSATION, 1):
        label = f"{NAMES[sender]} → {NAMES[recipient]}"
        print(f"[{i}/{len(CONVERSATION)}] {label}: {text}")

        if not args.dry_run:
            try:
                send_dm(sender, recipient, text)
            except Exception as e:
                print(f"  ERROR: {e}")
                print("  Is the API server running? uvicorn api.server:app --reload --port 8000")
                return

            if i < len(CONVERSATION):
                time.sleep(args.delay)

    print("\nDone. Open the DM view in the UI to see the conversation.")
    if not args.dry_run:
        print("Alice's view: http://localhost:3000 → switch to Alice → click Bob in DMs")
        print("Bob's view:   http://localhost:3000 (new tab) → switch to Bob → click Alice in DMs")


if __name__ == "__main__":
    main()
