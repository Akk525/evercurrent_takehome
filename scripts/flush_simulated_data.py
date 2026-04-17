"""
Flush simulated data written by simulate_thread_convo.py and simulate_dm.py.

By default clears thread replies only.
Pass --dms to also clear DM messages.
Pass --all to clear both + reset issue_memory.db.

Usage:
    python scripts/flush_simulated_data.py
    python scripts/flush_simulated_data.py --dms
    python scripts/flush_simulated_data.py --all
"""

import argparse
import sqlite3
from pathlib import Path

DIGEST_DB = Path(__file__).parent.parent / "data" / "digest_state.db"
MEMORY_DB = Path(__file__).parent.parent / "data" / "issue_memory.db"

# Threads and users all simulation scripts write to
SIM_THREADS = {"m_030", "m_010", "m_001", "m_020"}
SIM_USERS   = {"u_alice", "u_bob", "u_evan", "u_fiona", "u_greg"}


def flush_thread_replies(con: sqlite3.Connection) -> int:
    cur = con.execute(
        "DELETE FROM thread_replies WHERE thread_id IN ({}) AND user_id IN ({})".format(
            ",".join("?" * len(SIM_THREADS)),
            ",".join("?" * len(SIM_USERS)),
        ),
        [*SIM_THREADS, *SIM_USERS],
    )
    return cur.rowcount


def flush_dm_messages(con: sqlite3.Connection) -> int:
    cur = con.execute(
        "DELETE FROM dm_messages WHERE sender_id IN ({})".format(
            ",".join("?" * len(SIM_USERS))
        ),
        list(SIM_USERS),
    )
    return cur.rowcount


def main() -> None:
    parser = argparse.ArgumentParser(description="Flush simulated data from digest_state.db")
    parser.add_argument("--dms",  action="store_true", help="Also flush DM messages")
    parser.add_argument("--all",  action="store_true", help="Flush everything + reset issue_memory.db")
    args = parser.parse_args()

    if not DIGEST_DB.exists():
        print("digest_state.db not found — nothing to flush.")
        return

    con = sqlite3.connect(DIGEST_DB)
    try:
        replies_removed = flush_thread_replies(con)
        print(f"Thread replies removed : {replies_removed}")

        dms_removed = 0
        if args.dms or args.all:
            dms_removed = flush_dm_messages(con)
            print(f"DM messages removed   : {dms_removed}")

        con.commit()
    finally:
        con.close()

    if args.all and MEMORY_DB.exists():
        MEMORY_DB.unlink()
        print("issue_memory.db       : deleted (will be recreated on next server start)")

    total = replies_removed + dms_removed
    if total == 0:
        print("Nothing to flush — database was already clean.")
    else:
        print("\nDone. Restart the API server to pick up the clean state.")


if __name__ == "__main__":
    main()
