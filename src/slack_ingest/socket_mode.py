"""
Socket Mode client for event-driven Slack ingestion.

Socket Mode is the preferred mechanism for local development and internal apps:
  - No public URL required (no ngrok / reverse proxy)
  - Persistent WebSocket connection — Slack pushes events
  - Same event payloads as Events API

Requires:
  - SLACK_APP_TOKEN env var  (xapp-... token with connections:write scope)
  - SLACK_BOT_TOKEN env var  (xoxb-... token for API calls)
  - pip install slack-sdk[optional]  (includes aiohttp WebSocket support)

Usage (in FastAPI startup):
    manager = SocketModeManager(store, limiter, metrics)
    asyncio.create_task(manager.start())

Reference:
  https://api.slack.com/apis/connections/socket
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from .store import SlackIngestStore
from .rate_limits import RateLimiter
from .events import process_slack_event
from .models import SlackEventEnvelope

logger = logging.getLogger(__name__)


class SocketModeManager:
    """
    Manages a Slack Socket Mode connection and routes events to the ingest store.

    Not started automatically — must be explicitly started via asyncio.create_task(manager.start()).
    Does nothing if SLACK_APP_TOKEN is absent (graceful degradation).
    """

    def __init__(
        self,
        store: SlackIngestStore,
        limiter: RateLimiter,
        metrics=None,  # Optional[SlackIngestMetrics]
        app_token: Optional[str] = None,
    ):
        self.store = store
        self.limiter = limiter
        self.metrics = metrics
        self.app_token = app_token or os.environ.get("SLACK_APP_TOKEN", "").strip()
        self._client = None
        self._running = False

    def is_configured(self) -> bool:
        return bool(self.app_token)

    async def start(self) -> None:
        """
        Connect via Socket Mode and listen for events indefinitely.

        Reconnects automatically on connection drops.
        Exits cleanly if SLACK_APP_TOKEN is absent.
        """
        if not self.is_configured():
            logger.info("[socket_mode] SLACK_APP_TOKEN not set — Socket Mode disabled")
            return

        try:
            from slack_sdk.socket_mode.aiohttp import SocketModeClient
            from slack_sdk.socket_mode.response import SocketModeResponse
            from slack_sdk.socket_mode.request import SocketModeRequest
        except ImportError:
            logger.warning(
                "[socket_mode] slack-sdk aiohttp support not available. "
                "Install with: pip install 'slack-sdk[optional]'"
            )
            return

        self._running = True
        logger.info("[socket_mode] Connecting to Slack via Socket Mode...")

        while self._running:
            try:
                client = SocketModeClient(app_token=self.app_token)

                async def _listener(sock_client, req: SocketModeRequest) -> None:
                    # Acknowledge immediately — Slack requires < 3s ACK
                    resp = SocketModeResponse(envelope_id=req.envelope_id)
                    await sock_client.send_socket_mode_response(resp)

                    payload = req.payload
                    if not isinstance(payload, dict):
                        return

                    try:
                        envelope = SlackEventEnvelope.model_validate(payload)
                        process_slack_event(envelope, self.store, self.metrics)
                    except Exception as e:
                        logger.error("[socket_mode] Error processing event: %s", e, exc_info=True)

                client.socket_mode_request_listeners.append(_listener)
                self._client = client

                await client.connect()
                logger.info("[socket_mode] Connected. Listening for events...")

                # Keep alive — the SDK handles reconnects internally
                while self._running:
                    await asyncio.sleep(30)

            except Exception as e:
                logger.error("[socket_mode] Connection error: %s — reconnecting in 10s", e)
                await asyncio.sleep(10)

    def stop(self) -> None:
        self._running = False
        if self._client:
            try:
                asyncio.create_task(self._client.close())
            except Exception:
                pass
