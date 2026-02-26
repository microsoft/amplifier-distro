"""Slack Bridge App - connects Slack to Amplifier sessions.

This is the app plugin entry point for the distro server. It registers
FastAPI routes for:
- Slack Events API webhook (/events)
- Slack slash commands (/commands)
- Bridge management API (/status, /sessions, /discover)
- Simulator endpoints (when in simulator mode)

Architecture:
    Slack -> POST /apps/slack/events -> SlackEventHandler
        -> CommandHandler (for @mentions and commands)
        -> SlackSessionManager -> SessionBackend -> Amplifier

Configuration:
    Set SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET, SLACK_HUB_CHANNEL_ID
    environment variables, or use simulator mode for testing.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse

from amplifier_distro.server.app import AppManifest

from .backend import MockBackend, SessionBackend
from .client import MemorySlackClient, SlackClient
from .commands import CommandHandler
from .config import SlackConfig
from .discovery import AmplifierDiscovery
from .events import SlackEventHandler
from .sessions import SlackSessionManager
from .setup import router as setup_router
from .simulator import router as simulator_router

logger = logging.getLogger(__name__)

router = APIRouter()

# --- Global bridge state (initialized on startup) ---

_state: dict[str, Any] = {}
_state_lock = threading.Lock()


def _get_state() -> dict[str, Any]:
    """Get the initialized bridge state."""
    with _state_lock:
        if not _state:
            raise RuntimeError("Slack bridge not initialized. Call on_startup() first.")
        return _state


def initialize(
    config: SlackConfig | None = None,
    client: SlackClient | None = None,
    backend: SessionBackend | None = None,
    discovery: AmplifierDiscovery | None = None,
) -> dict[str, Any]:
    """Initialize the bridge components.

    This is separated from on_startup() so it can be called directly
    in tests with injected dependencies.

    Backend resolution order:
    1. Explicit backend parameter (tests)
    2. Shared server services backend (production)
    3. Fallback: create own MockBackend/BridgeBackend (standalone)
    """
    if config is None:
        config = SlackConfig.from_env()

    if discovery is None:
        discovery = AmplifierDiscovery()

    # Select client implementation
    if client is None:
        if config.simulator_mode or not config.is_configured:
            logger.info("Slack bridge starting in simulator mode")
            client = MemorySlackClient()
            config.simulator_mode = True
        else:
            from .client import HttpSlackClient

            client = HttpSlackClient(config.bot_token)

    # Select backend: prefer shared server backend
    if backend is None:
        try:
            from amplifier_distro.server.services import get_services

            backend = get_services().backend
            logger.info("Slack bridge using shared server backend")
        except RuntimeError:
            # Services not initialized (standalone mode or tests)
            if config.simulator_mode:
                backend = MockBackend()
            else:
                from .backend import BridgeBackend

                backend = BridgeBackend()
            logger.info("Slack bridge using own backend (standalone mode)")

    # Enable session persistence in production (not simulator/test mode)
    from .sessions import _default_persistence_path

    persistence_path = (
        _default_persistence_path() if not config.simulator_mode else None
    )
    session_manager = SlackSessionManager(client, backend, config, persistence_path)
    command_handler = CommandHandler(session_manager, discovery, config)
    event_handler = SlackEventHandler(client, session_manager, command_handler, config)

    state = {
        "config": config,
        "client": client,
        "backend": backend,
        "discovery": discovery,
        "session_manager": session_manager,
        "command_handler": command_handler,
        "event_handler": event_handler,
    }
    with _state_lock:
        _state.update(state)

    # Wire simulator hub when in simulator mode
    if config.simulator_mode and isinstance(client, MemorySlackClient):
        from .simulator import wire_client_to_hub

        wire_client_to_hub(client)

    return state


async def on_startup() -> None:
    """Initialize the Slack bridge on server startup."""
    initialize()
    with _state_lock:
        config: SlackConfig = _state["config"]
    logger.info(f"Slack bridge initialized (mode: {config.mode})")

    # Start Socket Mode connection if configured
    if config.socket_mode and config.is_configured:
        try:
            from .socket_mode import SocketModeAdapter

            with _state_lock:
                adapter = SocketModeAdapter(config, _state["event_handler"])
                _state["socket_adapter"] = adapter
            await adapter.start()
            logger.info("Socket Mode connection started")
        except ImportError:
            logger.error(
                "Socket Mode requires optional dependencies: "
                "pip install amplifier-distro[slack]  (aiohttp missing)"
            )
        except Exception:
            logger.exception("Socket Mode startup failed; Slack bridge degraded")


async def on_shutdown() -> None:
    """Clean up the Slack bridge on server shutdown."""
    with _state_lock:
        socket_adapter = _state.get("socket_adapter")
        session_manager = _state.get("session_manager")
        backend = _state.get("backend")

    # Stop Socket Mode connection if running
    if socket_adapter is not None:
        await socket_adapter.stop()

    if session_manager is not None and backend is not None:
        # End all active sessions
        for mapping in session_manager.list_active():
            try:
                await backend.end_session(mapping.session_id)
            except (RuntimeError, ValueError, ConnectionError, OSError):
                logger.exception(f"Error ending session {mapping.session_id}")

    with _state_lock:
        _state.clear()
    logger.info("Slack bridge shut down")


# --- HTML Pages ---


@router.get("/setup-ui", response_class=HTMLResponse)
async def setup_page() -> HTMLResponse:
    """Serve the Slack setup wizard page."""
    html_file = Path(__file__).parent / "static" / "slack-setup.html"
    if html_file.exists():
        return HTMLResponse(content=html_file.read_text())
    return HTMLResponse(
        content="<h1>Slack Setup</h1><p>slack-setup.html not found.</p>",
        status_code=500,
    )


# --- Slack Event Routes ---


@router.post("/events")
async def slack_events(request: Request) -> Response:
    """Slack Events API webhook endpoint.

    Handles:
    - URL verification challenge
    - Event callbacks (messages, mentions)
    """
    state = _get_state()
    handler: SlackEventHandler = state["event_handler"]
    config: SlackConfig = state["config"]

    body = await request.body()

    # Verify signature (skip in simulator mode)
    if not config.simulator_mode:
        timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
        signature = request.headers.get("X-Slack-Signature", "")
        if not handler.verify_signature(body, timestamp, signature):
            return Response(status_code=401, content="Invalid signature")

    payload = json.loads(body)
    result = await handler.handle_event_payload(payload)
    return Response(
        content=json.dumps(result),
        media_type="application/json",
    )


@router.post("/commands/{command}")
async def slack_command(command: str, request: Request) -> Response:
    """Slack slash command endpoint.

    Handles /amp <command> slash commands.
    """
    state = _get_state()
    cmd_handler: CommandHandler = state["command_handler"]

    form = await request.form()
    text = str(form.get("text", ""))
    user_id = str(form.get("user_id", ""))
    user_name = str(form.get("user_name", ""))
    channel_id = str(form.get("channel_id", ""))

    from .commands import CommandContext

    ctx = CommandContext(
        channel_id=channel_id,
        user_id=user_id,
        user_name=user_name,
        raw_text=text,
    )

    # Parse command args from the text
    parts = text.split() if text else []
    sub_command = parts[0] if parts else command
    args = parts[1:] if len(parts) > 1 else []

    result = await cmd_handler.handle(sub_command, args, ctx)

    # Slack expects a JSON response for slash commands
    response_data: dict[str, Any] = {}
    if result.blocks:
        response_data["blocks"] = result.blocks
    if result.text:
        response_data["text"] = result.text
    if result.ephemeral:
        response_data["response_type"] = "ephemeral"
    else:
        response_data["response_type"] = "in_channel"

    return Response(
        content=json.dumps(response_data),
        media_type="application/json",
    )


# --- Bridge Management API ---


@router.get("/status")
async def bridge_status() -> dict[str, Any]:
    """Bridge health and status."""
    state = _get_state()
    config: SlackConfig = state["config"]
    session_manager: SlackSessionManager = state["session_manager"]

    return {
        "status": "ok",
        "mode": config.mode,
        "hub_channel": config.hub_channel_name,
        "active_sessions": len(session_manager.list_active()),
        "is_configured": config.is_configured,
    }


@router.get("/sessions")
async def list_bridge_sessions() -> list[dict[str, Any]]:
    """List active session mappings."""
    state = _get_state()
    session_manager: SlackSessionManager = state["session_manager"]

    return [
        {
            "session_id": m.session_id,
            "channel_id": m.channel_id,
            "thread_ts": m.thread_ts,
            "project_id": m.project_id,
            "description": m.description,
            "created_by": m.created_by,
            "is_active": m.is_active,
        }
        for m in session_manager.list_active()
    ]


@router.get("/discover")
async def discover_local_sessions(
    limit: int = 20,
    project: str | None = None,
) -> list[dict[str, Any]]:
    """Discover local Amplifier sessions on the filesystem."""
    state = _get_state()
    discovery: AmplifierDiscovery = state["discovery"]

    sessions = discovery.list_sessions(limit=limit, project_filter=project)
    return [
        {
            "session_id": s.session_id,
            "project": s.project,
            "project_path": s.project_path,
            "date_str": s.date_str,
            "name": s.name,
            "description": s.description,
        }
        for s in sessions
    ]


@router.get("/projects")
async def list_projects() -> list[dict[str, Any]]:
    """List known projects."""
    state = _get_state()
    discovery: AmplifierDiscovery = state["discovery"]

    projects = discovery.list_projects()
    return [
        {
            "project_id": p.project_id,
            "project_name": p.project_name,
            "project_path": p.project_path,
            "session_count": p.session_count,
            "last_active": p.last_active,
        }
        for p in projects
    ]


# --- Setup Routes (always available for configuration) ---

router.include_router(setup_router)

# --- Simulator Routes (always included, serves UI in simulator mode) ---

router.include_router(simulator_router)


# --- Manifest ---


manifest = AppManifest(
    name="slack",
    description="Slack bridge - connects Slack channels to Amplifier sessions",
    version="0.1.0",
    router=router,
    on_startup=on_startup,
    on_shutdown=on_shutdown,
)
