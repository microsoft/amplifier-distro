"""Voice App - GA Realtime API routes.

Architecture:
    Browser (vanilla JS / React)
        -> WebRTC audio (Opus codec)
        -> OpenAI Realtime API (direct connection, no audio through server)
            -> Native speech-to-speech (no separate STT/TTS)
            -> Function calling for tools
        <- Audio response streamed back via WebRTC

    Backend (this module):
        GET  /                          - Voice UI static/index.html
        GET  /static/vendor.js          - Bundled vendor JS
        GET  /api/status                - Voice service status
        GET  /session        (auth)     - GA ephemeral client_secret token
        POST /sdp                       - WebRTC SDP offer/answer exchange
        GET  /events         (csrf)     - SSE event stream from active connection
        POST /sessions       (auth)     - Create Amplifier session + VoiceConnection
        POST /sessions/{id}/resume (auth) - Reconnect after disconnect
        POST /sessions/{id}/transcript (auth) - Batch sync transcript entries
        POST /sessions/{id}/end  (auth) - End session permanently
        GET  /sessions       (auth)     - List conversations (from repo index)
        POST /tools/execute  (auth)     - Execute a VOICE_TOOL
        POST /cancel         (auth)     - Cancel running session (graceful/immediate)

Single-user design: one _active_connection at a time, no parallel voice sessions.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
)

from amplifier_distro.server.app import AppManifest
from amplifier_distro.server.apps.voice.connection import VoiceConnection
from amplifier_distro.server.apps.voice.transcript.models import (
    TranscriptEntry,
    VoiceConversation,
)
from amplifier_distro.server.apps.voice.transcript.repository import (
    VoiceConversationRepository,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Module-level state: single-user, no parallel voice sessions
# ---------------------------------------------------------------------------

_active_connection: VoiceConnection | None = None

# Test-injection overrides (set these in tests; None = use real services)
_backend_override: Any = None
_repo_override: Any = None

# ---------------------------------------------------------------------------
# Session ID validation
# ---------------------------------------------------------------------------

_VALID_SESSION_ID = re.compile(r"^[a-zA-Z0-9_\-]+$")

# ---------------------------------------------------------------------------
# VOICE_TOOLS
# ---------------------------------------------------------------------------

VOICE_TOOLS: list[dict[str, Any]] = [
    {
        "name": "delegate",
        "description": "Delegate a task to the Amplifier agent with the given instruction.",  # noqa: E501
        "parameters": {
            "type": "object",
            "properties": {
                "instruction": {
                    "type": "string",
                    "description": "The instruction to pass to Amplifier.",
                }
            },
            "required": ["instruction"],
        },
    },
    {
        "name": "cancel_current_task",
        "description": "Cancel the currently running Amplifier task.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "pause_replies",
        "description": "Pause assistant replies until resume_replies is called.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "resume_replies",
        "description": "Resume assistant replies after pause_replies.",
        "parameters": {"type": "object", "properties": {}},
    },
]

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_backend() -> Any:
    """Return the active session backend (overridable for tests)."""
    if _backend_override is not None:
        return _backend_override
    from amplifier_distro.server.services import get_services

    return get_services().backend


def _get_repo() -> VoiceConversationRepository:
    """Return the voice conversation repository (overridable for tests)."""
    if _repo_override is not None:
        return _repo_override
    return VoiceConversationRepository()


def _get_voice_config() -> dict[str, Any]:
    """Load voice config from environment, with safe defaults."""
    return {
        "voice": os.environ.get("AMPLIFIER_VOICE_VOICE", "ash"),
        "model": os.environ.get("AMPLIFIER_VOICE_MODEL", "gpt-4o-realtime-preview"),
        "instructions": os.environ.get("AMPLIFIER_VOICE_INSTRUCTIONS", ""),
        "assistant_name": os.environ.get("AMPLIFIER_VOICE_ASSISTANT_NAME", "Amplifier"),
    }


def _get_workspace_root() -> Path:
    """Resolve workspace root from environment, falling back to home dir."""
    workspace = os.environ.get("AMPLIFIER_WORKSPACE_ROOT", "")
    if workspace:
        return Path(workspace).expanduser().resolve()
    return Path.home()


# ---------------------------------------------------------------------------
# Auth + CSRF dependencies (callable for use with Depends or direct await)
# ---------------------------------------------------------------------------


async def _require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Auth: no-op when AMPLIFIER_SERVER_API_KEY unset; HMAC compare_digest when set."""
    api_key = os.environ.get("AMPLIFIER_SERVER_API_KEY", "")
    if not api_key:
        return  # no-op: open access
    if x_api_key is None or not hmac.compare_digest(x_api_key, api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


async def _check_origin(origin: str | None = Header(default=None)) -> None:
    """CSRF: allow localhost/127.0.0.1; 403 for any other origin; no origin = allow."""
    if origin is None:
        return  # no origin header → allow
    if "localhost" in origin or "127.0.0.1" in origin:
        return  # trusted local origin
    raise HTTPException(status_code=403, detail="CSRF: origin not allowed")


def _validate_session_id(session_id: str) -> None:
    """Validate that session_id contains only safe characters.

    Raises HTTP 400 if the pattern doesn't match.
    """
    if not _VALID_SESSION_ID.match(session_id):
        raise HTTPException(status_code=400, detail="Invalid session_id format")


# ---------------------------------------------------------------------------
# Routes: static / UI
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """Serve static/index.html (200 with placeholder if not built yet)."""
    html_path = Path(__file__).parent / "static" / "index.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text())
    return HTMLResponse(
        content=(
            "<!-- Voice UI not built yet -->"
            "<html><body><h1>Amplifier Voice</h1>"
            "<p>Run <code>npm run build</code> to build the UI.</p></body></html>"
        ),
        status_code=200,
    )


@router.get("/static/vendor.js", response_model=None)
async def vendor_js():
    """Serve vendor.js (404 with comment if not built)."""
    js_path = Path(__file__).parent / "static" / "vendor.js"
    if js_path.exists():
        from fastapi.responses import Response

        return Response(
            content=js_path.read_bytes(), media_type="application/javascript"
        )
    return PlainTextResponse(
        content="// vendor.js not built yet - run npm run build",
        status_code=404,
    )


# ---------------------------------------------------------------------------
# Routes: status
# ---------------------------------------------------------------------------


@router.get("/api/status")
async def voice_status() -> JSONResponse:
    """Voice service status (no auth required)."""
    api_key = os.environ.get("OPENAI_API_KEY")
    vcfg = _get_voice_config()
    return JSONResponse(
        content={
            "status": "ready" if api_key else "unconfigured",
            "api_key_set": bool(api_key),
            "model": vcfg["model"],
            "voice": vcfg["voice"],
            "assistant_name": vcfg["assistant_name"],
            "turn_server": None,
        }
    )


# ---------------------------------------------------------------------------
# Routes: signaling (session token + SDP)
# ---------------------------------------------------------------------------


@router.get("/session")
async def create_session_token(
    x_api_key: str | None = Header(default=None),
) -> JSONResponse:
    """Return ephemeral client_secret for the GA Realtime API.

    Stub mode: returns stub_voice_client_secret().
    Real mode: calls realtime.create_client_secret().
    """
    await _require_api_key(x_api_key)

    from amplifier_distro.server.stub import is_stub_mode, stub_voice_client_secret

    if is_stub_mode():
        token = stub_voice_client_secret()
        return JSONResponse(content={"value": token})

    from amplifier_distro.server.apps.voice import realtime as rt

    vcfg = _get_voice_config()
    config = rt.VoiceConfig(
        model=vcfg["model"],
        voice=vcfg["voice"],
        instructions=vcfg["instructions"],
        openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
    )
    token = await rt.create_client_secret(config)
    return JSONResponse(content={"value": token})


@router.post("/sdp", response_model=None)
async def exchange_sdp(request: Request) -> PlainTextResponse | JSONResponse:
    """Exchange WebRTC SDP offer/answer.

    Stub mode: returns stub_voice_sdp().
    Real mode: calls realtime.exchange_sdp() using Bearer ephemeral token.
    """
    from amplifier_distro.server.stub import is_stub_mode, stub_voice_sdp

    if is_stub_mode():
        return PlainTextResponse(content=stub_voice_sdp(), media_type="application/sdp")

    offer_sdp = (await request.body()).decode(errors="replace")
    if not offer_sdp:
        return JSONResponse(
            status_code=400, content={"error": "SDP offer body required"}
        )

    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return JSONResponse(
            status_code=401, content={"error": "Bearer ephemeral token required"}
        )

    ephemeral_token = auth[len("Bearer ") :]
    vcfg = _get_voice_config()

    from amplifier_distro.server.apps.voice import realtime as rt

    sdp_answer = await rt.exchange_sdp(offer_sdp, ephemeral_token, vcfg["model"])
    return PlainTextResponse(content=sdp_answer, media_type="application/sdp")


# ---------------------------------------------------------------------------
# Routes: SSE event stream
# ---------------------------------------------------------------------------


@router.get("/events")
async def events_stream(
    request: Request,
    origin: str | None = Header(default=None),
) -> StreamingResponse:
    """Server-Sent Events stream from the active VoiceConnection.

    CSRF-checked via Origin header.
    Heartbeat every 5 s when no active connection.
    """
    await _check_origin(origin)

    async def _generate():
        try:
            while True:
                if await request.is_disconnected():
                    break
                conn = _active_connection
                if conn is not None:
                    try:
                        event = await asyncio.wait_for(
                            conn.event_queue.get(),
                            timeout=5.0,
                        )
                        import json as _json

                        yield f"data: {_json.dumps(event)}\n\n"
                    except TimeoutError:
                        yield ": heartbeat\n\n"
                else:
                    yield ": heartbeat\n\n"
                    await asyncio.sleep(5.0)
        except (asyncio.CancelledError, GeneratorExit):
            pass

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Routes: session lifecycle
# ---------------------------------------------------------------------------


@router.post("/sessions")
async def create_session(
    request: Request,
    x_api_key: str | None = Header(default=None),
) -> JSONResponse:
    """Create an Amplifier session and wire it to a VoiceConnection.

    Body (optional JSON):
        workspace_root: str  - working directory for the session
    """
    await _require_api_key(x_api_key)

    global _active_connection

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}

    workspace_root = body.get("workspace_root", str(_get_workspace_root()))

    backend = _get_backend()
    repo = _get_repo()

    conn = VoiceConnection(repository=repo, backend=backend)
    session_id = await conn.create(workspace_root)

    # Persist a VoiceConversation record
    conv = VoiceConversation(
        id=session_id,
        title=f"Voice session {session_id[:8]}",
        status="active",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    repo.create_conversation(conv)

    _active_connection = conn
    logger.info("Voice session created: %s", session_id)
    return JSONResponse(content={"session_id": session_id})


@router.post("/sessions/{session_id}/resume")
async def resume_session(
    session_id: str,
    request: Request,
    x_api_key: str | None = Header(default=None),
) -> JSONResponse:
    """Reconnect after a disconnect; returns fresh client_secret + context_to_inject."""
    await _require_api_key(x_api_key)
    _validate_session_id(session_id)

    global _active_connection

    backend = _get_backend()
    repo = _get_repo()

    # Reconnect backend (restores session state)
    await backend.reconnect(session_id)

    # Pull transcript context for the Realtime API
    context = repo.get_resumption_context(session_id)

    # Obtain a fresh ephemeral token
    from amplifier_distro.server.stub import is_stub_mode, stub_voice_client_secret

    if is_stub_mode():
        client_secret = stub_voice_client_secret()
    else:
        from amplifier_distro.server.apps.voice import realtime as rt

        vcfg = _get_voice_config()
        config = rt.VoiceConfig(
            model=vcfg["model"],
            voice=vcfg["voice"],
            instructions=vcfg["instructions"],
            openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
        )
        client_secret = await rt.create_client_secret(config)

    # Recreate the VoiceConnection with a fresh queue
    conn = VoiceConnection(repository=repo, backend=backend)
    workspace_root = str(_get_workspace_root())
    await conn.create(workspace_root)
    _active_connection = conn

    logger.info("Voice session resumed: %s", session_id)
    return JSONResponse(
        content={
            "client_secret": client_secret,
            "context_to_inject": context,
        }
    )


@router.post("/sessions/{session_id}/transcript")
async def sync_transcript(
    session_id: str,
    request: Request,
    x_api_key: str | None = Header(default=None),
) -> JSONResponse:
    """Batch-add TranscriptEntry records for a session."""
    await _require_api_key(x_api_key)
    _validate_session_id(session_id)

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body"})

    entries_data: list[dict[str, Any]] = body.get("entries", [])
    repo = _get_repo()
    now = datetime.now(UTC)

    entries = [
        TranscriptEntry(
            id=str(uuid4()),
            conversation_id=session_id,
            role=e.get("role", "user"),
            content=e.get("content", ""),
            created_at=now,
            item_id=e.get("item_id"),
            tool_name=e.get("tool_name"),
            call_id=e.get("call_id"),
        )
        for e in entries_data
    ]
    repo.add_entries(session_id, entries)

    return JSONResponse(content={"synced": len(entries)})


@router.post("/sessions/{session_id}/end")
async def end_session(
    session_id: str,
    request: Request,
    x_api_key: str | None = Header(default=None),
) -> JSONResponse:
    """End a session permanently."""
    await _require_api_key(x_api_key)
    _validate_session_id(session_id)

    global _active_connection

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}

    reason: str = body.get("reason", "user_ended")
    backend = _get_backend()
    repo = _get_repo()

    await backend.end_session(session_id)
    repo.end_conversation(session_id, reason)
    _active_connection = None

    logger.info("Voice session ended: %s (reason=%s)", session_id, reason)
    return JSONResponse(content={"ended": True, "session_id": session_id})


@router.get("/sessions")
async def list_sessions(
    x_api_key: str | None = Header(default=None),
) -> JSONResponse:
    """Return the list of VoiceConversations from the repository index."""
    await _require_api_key(x_api_key)
    repo = _get_repo()
    return JSONResponse(content=repo.list_conversations())


# ---------------------------------------------------------------------------
# Routes: tool execution
# ---------------------------------------------------------------------------


@router.post("/tools/execute")
async def execute_tool(
    request: Request,
    x_api_key: str | None = Header(default=None),
) -> JSONResponse:
    """Execute a VOICE_TOOL on behalf of the active voice session.

    Supported tools:
      - delegate: run instruction via backend.execute()
      - cancel_current_task: cancel active session
      - pause_replies / resume_replies: acknowledged (future implementation)
    """
    await _require_api_key(x_api_key)

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body"})

    name: str = body.get("name", "")
    arguments: dict[str, Any] = body.get("arguments", {})

    if not name:
        return JSONResponse(status_code=400, content={"error": "Missing 'name' field"})

    conn = _active_connection

    if name == "delegate":
        instruction = arguments.get("instruction", "")
        if not instruction:
            return JSONResponse(
                status_code=400, content={"error": "instruction required for delegate"}
            )
        if conn is None or conn.session_id is None:
            return JSONResponse(
                status_code=400, content={"error": "No active voice session"}
            )
        backend = _get_backend()
        await backend.execute(conn.session_id, instruction)
        return JSONResponse(content={"result": "delegated"})

    if name == "cancel_current_task":
        if conn is None:
            return JSONResponse(
                status_code=400, content={"error": "No active voice session"}
            )
        await conn.cancel()
        return JSONResponse(content={"result": "cancelled"})

    if name in ("pause_replies", "resume_replies"):
        return JSONResponse(content={"result": f"{name} acknowledged"})

    return JSONResponse(status_code=400, content={"error": f"Unknown tool: {name}"})


# ---------------------------------------------------------------------------
# Routes: cancel
# ---------------------------------------------------------------------------


@router.post("/cancel")
async def cancel_session(
    request: Request,
    x_api_key: str | None = Header(default=None),
) -> JSONResponse:
    """Cancel the running session.

    Body:
        session_id: str   - target session
        immediate: bool   - False = graceful (single-click), True = immediate
    """
    await _require_api_key(x_api_key)

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body"})

    session_id: str = body.get("session_id", "")
    immediate: bool = body.get("immediate", False)

    backend = _get_backend()
    await backend.cancel_session(session_id, immediate=immediate)

    return JSONResponse(content={"cancelled": True, "session_id": session_id})


# ---------------------------------------------------------------------------
# AppManifest
# ---------------------------------------------------------------------------

manifest = AppManifest(
    name="voice",
    description="Amplifier voice interface via OpenAI Realtime API (GA)",
    version="1.0.0",
    router=router,
)
