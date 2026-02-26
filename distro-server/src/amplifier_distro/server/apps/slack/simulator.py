"""Slack Simulator - WebSocket hub and routes for the test UI.

When the bridge runs in simulator mode, this module provides:
- A WebSocket endpoint that streams all bot messages to connected browsers
- A route to serve the simulator HTML page
- A simulated user directory

The simulator lets you interact with the Slack bridge through a web UI
that looks like Slack, without needing a real Slack workspace.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from .client import MemorySlackClient, SentMessage

logger = logging.getLogger(__name__)

# Strong references to fire-and-forget tasks so they aren't garbage-collected.
_background_tasks: set[Any] = set()

router = APIRouter(prefix="/simulator")


# --- Simulated Users ---

SIMULATED_USERS = {
    "U_ALICE": {"name": "alice", "display_name": "Alice", "avatar_color": "#e74c3c"},
    "U_BOB": {"name": "bob", "display_name": "Bob", "avatar_color": "#3498db"},
    "U_CAROL": {"name": "carol", "display_name": "Carol", "avatar_color": "#2ecc71"},
    "U_AMP_BOT": {
        "name": "amplifier",
        "display_name": "Amplifier",
        "avatar_color": "#9b59b6",
        "is_bot": True,
    },
}


# --- WebSocket Hub ---


@dataclass
class SimulatorHub:
    """Manages WebSocket connections for the simulator UI."""

    connections: list[WebSocket] = field(default_factory=list)
    message_history: list[dict[str, Any]] = field(default_factory=list)
    _max_history: int = 500

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.connections.append(ws)
        # Send message history to new connection
        for msg in self.message_history:
            await ws.send_json(msg)
        logger.info(f"Simulator client connected ({len(self.connections)} total)")

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self.connections:
            self.connections.remove(ws)
        logger.info(f"Simulator client disconnected ({len(self.connections)} total)")

    async def broadcast(self, event: dict[str, Any]) -> None:
        """Send an event to all connected simulator clients."""
        self.message_history.append(event)
        if len(self.message_history) > self._max_history:
            self.message_history = self.message_history[-self._max_history :]

        dead: list[WebSocket] = []
        for ws in self.connections:
            try:
                await ws.send_json(event)
            except (ConnectionError, OSError, RuntimeError):
                logger.debug("Failed to send to WebSocket client", exc_info=True)
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


# Module-level hub instance
_hub = SimulatorHub()


def get_hub() -> SimulatorHub:
    return _hub


# --- MemorySlackClient Integration ---


def wire_client_to_hub(client: MemorySlackClient) -> None:
    """Connect a MemorySlackClient's output to the simulator hub.

    When the bridge sends a message through the MemorySlackClient,
    this callback converts it to a simulator event and broadcasts
    it to all connected browser clients.
    """
    import asyncio

    def on_message(msg: SentMessage) -> None:
        event = {
            "type": "bot_message",
            "channel": msg.channel,
            "text": msg.text,
            "thread_ts": msg.thread_ts,
            "blocks": msg.blocks,
            "ts": msg.ts,
            "user_id": "U_AMP_BOT",
            "user_name": "Amplifier",
            "timestamp": time.time(),
        }
        # Schedule the async broadcast from sync callback
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(_hub.broadcast(event))
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)
        except RuntimeError:
            # No running loop - skip (happens in tests)
            pass

    client.on_message_sent = on_message
    logger.info("Simulator wired to MemorySlackClient")


# --- Routes ---

STATIC_DIR = Path(__file__).parent / "static"


@router.get("", response_class=HTMLResponse)
async def simulator_page() -> HTMLResponse:
    """Serve the simulator HTML page."""
    html_path = STATIC_DIR / "simulator.html"
    if not html_path.exists():
        return HTMLResponse("<h1>Simulator HTML not found</h1>", status_code=404)
    return HTMLResponse(html_path.read_text())


@router.websocket("/ws")
async def simulator_ws(ws: WebSocket) -> None:
    """WebSocket endpoint for real-time simulator events."""
    await _hub.connect(ws)
    try:
        while True:
            # Receive user messages from the browser
            data = await ws.receive_json()
            event_type = data.get("type", "")

            if event_type == "ping":
                await ws.send_json({"type": "pong"})
            elif event_type == "user_message":
                # Browser is telling us about a user message it sent via REST
                # Broadcast to all clients so other tabs see it too
                await _hub.broadcast(data)
            else:
                logger.debug(f"Simulator received unknown event: {event_type}")
    except WebSocketDisconnect:
        _hub.disconnect(ws)
    except (ConnectionError, OSError, RuntimeError):
        logger.exception("Simulator WebSocket error")
        _hub.disconnect(ws)


@router.get("/users")
async def list_users() -> dict[str, Any]:
    """List simulated users."""
    return {"users": SIMULATED_USERS}


@router.post("/send")
async def simulator_send(request: dict[str, Any]) -> dict[str, Any]:
    """Send a simulated Slack event to the bridge.

    This is the browser's way of sending messages. It constructs
    a Slack-compatible event payload and forwards it to the bridge's
    event handler.

    Body:
        channel_id: str
        user_id: str
        text: str
        thread_ts: str | None
    """
    from . import _state

    if not _state:
        return {"error": "Bridge not initialized"}

    event_handler = _state["event_handler"]
    channel_id = request.get("channel_id", "C_HUB")
    user_id = request.get("user_id", "U_ALICE")
    text = request.get("text", "")
    thread_ts = request.get("thread_ts")

    ts = f"{time.time():.6f}"

    # Build Slack-compatible event payload
    event: dict[str, Any] = {
        "type": "message",
        "text": text,
        "user": user_id,
        "channel": channel_id,
        "ts": ts,
    }
    if thread_ts:
        event["thread_ts"] = thread_ts

    # Check if this is a command (@mention or slash-like)
    bot_user_id = await _state["client"].get_bot_user_id()
    if f"<@{bot_user_id}>" in text or text.startswith("/amp "):
        event["type"] = "app_mention"

    payload = {"type": "event_callback", "event": event}

    # Broadcast the user message to all simulator clients
    user_info = SIMULATED_USERS.get(user_id, {"display_name": user_id})
    user_event = {
        "type": "user_message",
        "channel": channel_id,
        "text": text,
        "thread_ts": thread_ts,
        "ts": ts,
        "user_id": user_id,
        "user_name": user_info.get("display_name", user_id),
        "timestamp": time.time(),
    }
    await _hub.broadcast(user_event)

    # Process through the bridge
    result = await event_handler.handle_event_payload(payload)
    return {"ok": True, "ts": ts, "result": result}


@router.get("/channels")
async def list_channels() -> dict[str, Any]:
    """List simulated channels."""
    from . import _state

    if not _state:
        return {"channels": []}

    config = _state["config"]
    session_manager = _state["session_manager"]

    channels = [
        {
            "id": config.hub_channel_id or "C_HUB",
            "name": config.hub_channel_name or "amplifier",
            "type": "hub",
            "topic": "Talk to Amplifier here",
        }
    ]

    # Add breakout channels from active sessions
    channels.extend(
        {
            "id": mapping.channel_id,
            "name": f"amp-{mapping.session_id[:8]}",
            "type": "session",
            "topic": mapping.description or "Amplifier session",
        }
        for mapping in session_manager.list_active()
        if mapping.channel_id != (config.hub_channel_id or "C_HUB")
    )

    return {"channels": channels}
