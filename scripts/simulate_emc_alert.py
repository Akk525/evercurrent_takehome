"""
Simulate an urgent EMC pre-compliance failure escalation on the #test-and-validation channel.

This is completely unrelated to the Alice/Bob hardware thread — different domain
(regulatory/certification), different participants (Evan, Fiona, Greg), different channel thread.

The EMC failure is launch-blocking: the device radiates 12 dB above Class B limits,
the lab re-test slot is in 48 hours, and the root cause is unknown.
High urgency + blocker type → should surface prominently in the digest.

Default target: m_020 in #test-and-validation
Usage:
    python scripts/simulate_emc_alert.py
    python scripts/simulate_emc_alert.py --delay 2.0
    python scripts/simulate_emc_alert.py --dry-run
"""

import argparse
import time
import urllib.request
import urllib.parse
import json

API_URL = "http://localhost:8000"

THREAD_ID = "m_020"
THREAD_LABEL = "#test-and-validation — Thermal cycling / Rev C PCBA"

REPLIES = [
    ("u_evan",  "Escalating this to the broader group — we just got the EMC pre-compliance results back from the external lab and they are not good. The device is 12 dB over the Class B radiated emission limit at 480 MHz. That is not a margin problem, that is a fundamental issue."),
    ("u_fiona", "12 dB over? That is a full order of magnitude in field strength. What was the test configuration — were the cables managed the same way as our internal bench setup?"),
    ("u_evan",  "Lab used the standard CISPR 32 setup. Cables dressed per their standard harness, device in normal operating mode, no special shielding. This is the unmodified build. The 480 MHz spike is consistent with the USB 3.0 spread-spectrum clock — I think we have an unfiltered harmonic leaking through the chassis seam."),
    ("u_greg",  "If it is the chassis seam then it is a mechanical fix, not a board respin. We can try conductive gasket tape on that joint before the re-test. Do we still have the lab slot booked?"),
    ("u_evan",  "Re-test slot is 48 hours from now. That is the last available slot before the certification freeze. If we miss it we are looking at a 6-week delay minimum — the lab is fully booked after that."),
    ("u_fiona", "We need to loop in Greg and Carlos on the mechanical side immediately. The gasket approach might work but we need someone who can confirm the chassis geometry has enough surface contact at that joint. This cannot be a guess."),
    ("u_greg",  "I can pull the chassis drawings tonight. If the seam runs the full length of the bottom panel we have enough surface area for a proper gasket. But someone needs to source the material and have it on-site by tomorrow morning — that is a procurement call right now."),
    ("u_evan",  "Flagging this as a full stop blocker on certification. I am going to send a formal hold notice to the launch planning thread. Nobody should be counting on the current ship date until we confirm the re-test passes. Diana and Alice need to know."),
    ("u_fiona", "Agreed. Also — we should capture this in the test log with the raw scan data attached. If the gasket fix works we need documented evidence for the technical file. The cert body will ask for it."),
    ("u_evan",  "Already exported the scan. I will drop it in the shared folder and link it here. Greg — can you confirm chassis drawing revision and gasket part number by 9am tomorrow? We have one shot at this."),
]

NAMES = {
    "u_evan":  "Evan",
    "u_fiona": "Fiona",
    "u_greg":  "Greg",
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
    parser = argparse.ArgumentParser(
        description="Simulate an urgent EMC pre-compliance failure escalation"
    )
    parser.add_argument("--delay", type=float, default=1.0,
                        help="Seconds between replies (default: 1.0)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print replies without sending")
    args = parser.parse_args()

    print(f"Target thread : {THREAD_ID} — {THREAD_LABEL}")
    print(f"Participants  : Evan, Fiona, Greg")
    print(f"Domain        : EMC pre-compliance failure (certification blocker)")
    print(f"Posting       : {len(REPLIES)} replies")
    if args.dry_run:
        print("(dry run — not sending)\n")
    else:
        print(f"(delay: {args.delay}s between replies)\n")

    for i, (sender, text) in enumerate(REPLIES, 1):
        print(f"[{i}/{len(REPLIES)}] {NAMES[sender]}: {text[:80]}{'...' if len(text) > 80 else ''}")

        if not args.dry_run:
            try:
                post_reply(THREAD_ID, sender, text)
            except Exception as e:
                print(f"  ERROR: {e}")
                print("  Is the API server running? uvicorn api.server:app --reload --port 8000")
                return

            if i < len(REPLIES):
                time.sleep(args.delay)

    if not args.dry_run:
        print("\nDone. Digest will refresh in ~15 seconds.")
        print("Signals this should trigger:")
        print("  - event_type: blocker (12 dB over limit, 6-week delay risk)")
        print("  - urgency: high (48-hour window, last lab slot)")
        print("  - cross_functional: high (Evan + Fiona + Greg across test/mech/procurement)")
        print("  - unresolved: high (root cause confirmed, fix not yet validated)")
        print()
        print("Check Digest Bot for Evan, Fiona, and Greg — this should rank near the top.")
        print("Compare with Alice's digest — she is mentioned but not a direct participant.")


if __name__ == "__main__":
    main()
