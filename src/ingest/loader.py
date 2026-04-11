"""
Load and normalize raw Slack mock data from JSON files.

Nothing fancy here — just clean deserialization into typed models.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.models import SlackWorkspace, SlackMessage, SlackThread, SlackUser, SlackChannel


def load_workspace(data_dir: Path) -> SlackWorkspace:
    """
    Load all Slack entities from the given directory.

    Expected files:
      users.json    — list of SlackUser objects
      channels.json — list of SlackChannel objects
      messages.json — list of SlackMessage objects
      threads.json  — list of SlackThread objects
    """
    users = _load_list(data_dir / "users.json", SlackUser)
    channels = _load_list(data_dir / "channels.json", SlackChannel)
    messages = _load_list(data_dir / "messages.json", SlackMessage)
    threads = _load_list(data_dir / "threads.json", SlackThread)

    return SlackWorkspace(
        users=users,
        channels=channels,
        messages=messages,
        threads=threads,
    )


def _load_list(path: Path, model_class) -> list:
    with open(path, "r") as f:
        raw = json.load(f)
    return [model_class.model_validate(item) for item in raw]
