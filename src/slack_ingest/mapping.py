"""
User identity mapping — Slack user IDs ↔ digest engine user IDs.

The digest engine uses opaque internal IDs (e.g. "u_alice").
Slack uses its own member IDs (e.g. "U012AB3CD").

This module resolves the mapping in both directions.

Acceptable MVP approach (as specified):
    1. SLACK_USER_MAP env var (JSON string) — highest priority
    2. config/slack_user_map.json file — fallback
    3. Empty mapping (graceful degradation — local mode works unchanged)

No full identity federation. Assumptions documented below.

Assumptions:
    - Mapping is configured manually by the workspace admin
    - Slack user IDs are stable (they don't change for a given member)
    - Engine user IDs are stable across pipeline runs
    - This module never calls Slack's users.list to auto-discover — that is a
      rate-limited read that should be done at startup only if explicitly triggered

Format (both env var and file):
    {
        "u_alice": "U012AB3CD",
        "u_bob":   "U034EF5GH",
        "u_carlos": "U056IJ7KL"
    }
    Keys = engine user IDs, values = Slack member IDs.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_CONFIG_FILE = Path(__file__).parent.parent.parent / "config" / "slack_user_map.json"


class UserIdentityMap:
    """
    Bidirectional mapping between engine user IDs and Slack member IDs.

    Usage:
        identity = UserIdentityMap.load()
        slack_id = identity.engine_to_slack("u_alice")   # "U012AB3CD"
        engine_id = identity.slack_to_engine("U012AB3CD") # "u_alice"
    """

    def __init__(self, engine_to_slack: dict[str, str]) -> None:
        self._fwd: dict[str, str] = engine_to_slack
        self._rev: dict[str, str] = {v: k for k, v in engine_to_slack.items()}

    @classmethod
    def load(cls) -> "UserIdentityMap":
        """
        Load user map from environment or config file.
        Returns an empty map (not None) if neither is configured.
        """
        # 1. Environment variable (highest priority)
        raw = os.environ.get("SLACK_USER_MAP", "").strip()
        if raw:
            try:
                mapping = json.loads(raw)
                if isinstance(mapping, dict):
                    logger.info(
                        "[identity] Loaded %d user mappings from SLACK_USER_MAP",
                        len(mapping),
                    )
                    return cls(mapping)
            except json.JSONDecodeError:
                logger.warning("[identity] SLACK_USER_MAP is not valid JSON — ignoring")

        # 2. Config file
        if _CONFIG_FILE.exists():
            try:
                mapping = json.loads(_CONFIG_FILE.read_text())
                if isinstance(mapping, dict):
                    logger.info(
                        "[identity] Loaded %d user mappings from %s",
                        len(mapping),
                        _CONFIG_FILE,
                    )
                    return cls(mapping)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("[identity] Could not load %s: %s", _CONFIG_FILE, e)

        logger.info("[identity] No user map configured — Slack delivery will require explicit mapping")
        return cls({})

    def engine_to_slack(self, engine_user_id: str) -> Optional[str]:
        """Return the Slack member ID for a given engine user ID, or None."""
        return self._fwd.get(engine_user_id)

    def slack_to_engine(self, slack_user_id: str) -> Optional[str]:
        """Return the engine user ID for a given Slack member ID, or None."""
        return self._rev.get(slack_user_id)

    def all_engine_ids(self) -> list[str]:
        return list(self._fwd.keys())

    def all_slack_ids(self) -> list[str]:
        return list(self._rev.keys())

    def is_empty(self) -> bool:
        return len(self._fwd) == 0

    def __repr__(self) -> str:
        return f"UserIdentityMap({len(self._fwd)} mappings)"

    def register(self, engine_id: str, slack_id: str) -> None:
        """Add or update a single mapping at runtime (not persisted)."""
        self._fwd[engine_id] = slack_id
        self._rev[slack_id] = engine_id
