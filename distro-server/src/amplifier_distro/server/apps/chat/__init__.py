"""Chat App — Rich WebSocket-based chat UI for amplifier-distro.

Successor to web_chat. Provides streaming text, thinking blocks,
tool call cards, sub-agent nesting, focus mode, and multi-session.

Routes:
    GET  /                         - Serves the chat HTML page
    GET  /vendor.js                - Serves vendored frontend bundle
    GET  /api/health               - Health check
    WS   /ws                       - WebSocket chat connection (Task 6)
    GET  /api/sessions/history     - List all disk-discovered sessions
    GET  /api/sessions/revisions   - Lightweight disk revision metadata (legacy)
    POST /api/sessions/revisions   - Diff revisions vs client-known state
    GET  /api/sessions             - List sessions (Task 11)
    GET  /api/sessions/{id}/transcript  - Transcript (Task 12)
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import types
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, Response

from amplifier_distro.conventions import (
    AMPLIFIER_HOME,
    PROJECTS_DIR,
    TRANSCRIPT_FILENAME,
)
from amplifier_distro.server.app import AppManifest
from amplifier_distro.server.apps.chat.session_history import (
    scan_session_revisions,
    scan_sessions,
)

logger = logging.getLogger(__name__)


def _require_api_key(
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
) -> None:
    """Verify api_key if configured. No-op when api_key is None."""
    api_key = os.environ.get("AMPLIFIER_SERVER_API_KEY")
    if api_key is None:
        return  # auth not enabled
    if not x_api_key or not hmac.compare_digest(str(x_api_key), str(api_key)):
        raise HTTPException(status_code=401, detail="Unauthorized")


router = APIRouter()

_static_dir = Path(__file__).parent / "static"

_VALID_SESSION_ID = re.compile(r"^[a-zA-Z0-9_\-]+$")


def _parse_session_id_set(values: list[str]) -> set[str]:
    """Validate session IDs and return a de-duplicated set."""
    out: set[str] = set()
    for raw in values:
        session_id = (raw or "").strip()
        if not session_id:
            continue
        if not _VALID_SESSION_ID.fullmatch(session_id):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid session ID format: {session_id!r}",
            )
        out.add(session_id)
    return out


def _load_transcript_payload(session_id: str) -> dict | None:
    """Load transcript JSON payload for one session from disk.

    Returns:
        dict payload when found, else None.
    Raises:
        OSError for filesystem/read failures.
    """
    projects_path = Path(AMPLIFIER_HOME).expanduser() / PROJECTS_DIR

    transcript_file: Path | None = None
    if projects_path.exists():
        for project_dir in projects_path.iterdir():
            if not project_dir.is_dir():
                continue
            sessions_subdir = project_dir / "sessions"
            candidate_dir = sessions_subdir if sessions_subdir.is_dir() else project_dir
            candidate = candidate_dir / session_id / TRANSCRIPT_FILENAME
            if candidate.exists():
                transcript_file = candidate
                break

    if transcript_file is None:
        return None

    messages = []
    with transcript_file.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if isinstance(entry, dict) and entry.get("role"):
                    messages.append(entry)
            except json.JSONDecodeError:
                continue

    stat = transcript_file.stat()
    last_updated = datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat()
    mtime_ns = getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))
    revision = f"{int(mtime_ns)}:{int(stat.st_size)}"

    return {
        "session_id": session_id,
        "transcript": messages,
        "last_updated": last_updated,
        "revision": revision,
    }


@router.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """Serve the chat interface."""
    html_file = _static_dir / "index.html"
    if html_file.exists():
        return HTMLResponse(content=html_file.read_text(encoding="utf-8"))
    return HTMLResponse(
        content=(
            "<html><body>"
            "<h1>Amplifier Chat</h1>"
            "<p>index.html not found. Run the vendor build step.</p>"
            "</body></html>"
        ),
        status_code=200,
    )


@router.get("/vendor.js")
async def vendor_js() -> Response:
    """Serve vendored frontend bundle (Preact + HTM + marked.js)."""
    vendor_file = _static_dir / "vendor.js"
    if vendor_file.exists():
        return Response(
            content=vendor_file.read_text(encoding="utf-8"),
            media_type="application/javascript",
        )
    return Response(
        content="// vendor.js not found — run the vendor build step\n",
        media_type="application/javascript",
        status_code=404,
    )


@router.get("/api/health")
async def health() -> dict:
    """Health check endpoint."""
    return {"status": "ok"}


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    """WebSocket endpoint — one connection per Amplifier session."""
    from amplifier_distro.server.apps.chat.connection import ChatConnection
    from amplifier_distro.server.services import get_services

    api_key = os.environ.get("AMPLIFIER_SERVER_API_KEY")
    host = getattr(ws.app.state, "host", "127.0.0.1")
    config = types.SimpleNamespace(
        server=types.SimpleNamespace(api_key=api_key, host=host)
    )

    try:
        services = get_services()
    except Exception:
        logger.exception("Services unavailable — closing WebSocket with 1011")
        await ws.accept()
        await ws.close(1011, "Internal server error")
        return

    conn = ChatConnection(ws, services.backend, config)  # type: ignore[arg-type]
    await conn.run()


@router.get("/api/sessions/history", dependencies=[Depends(_require_api_key)])
async def list_session_history(
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict:
    """Return lightweight metadata for all sessions discovered on disk."""
    sessions = await asyncio.to_thread(scan_sessions)
    sessions = [
        row
        for row in sessions
        if (row.get("message_count") or 0) > 0 or row.get("last_user_message")
    ]
    return {"sessions": sessions[:limit]}


@router.get("/api/sessions/revisions", dependencies=[Depends(_require_api_key)])
async def list_session_revisions(
    limit: int = Query(default=300, ge=1, le=5000),
    session_ids: str | None = Query(default=None, max_length=20000),
) -> dict:
    """Return revision signatures for disk-backed sessions (legacy GET form)."""
    wanted: set[str] | None = None
    if session_ids:
        wanted = _parse_session_id_set(session_ids.split(","))

    rows = await asyncio.to_thread(scan_session_revisions, wanted)
    return {"sessions": rows[:limit]}


@router.post("/api/sessions/revisions", dependencies=[Depends(_require_api_key)])
async def diff_session_revisions(request: Request) -> dict:
    """Return only changed/removed revisions compared to client-known revisions."""
    raw = await request.body()
    if not raw:
        body: dict = {}
    else:
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=400, detail="Request body must be valid JSON"
            ) from exc
        if not isinstance(body, dict):
            raise HTTPException(
                status_code=400, detail="Request body must be a JSON object"
            )

    raw_ids = body.get("session_ids")
    if raw_ids is None:
        wanted: set[str] | None = None
    elif isinstance(raw_ids, list):
        if not all(isinstance(v, str) for v in raw_ids):
            raise HTTPException(
                status_code=400, detail="'session_ids' must be a list of strings"
            )
        wanted = _parse_session_id_set(raw_ids)
    else:
        raise HTTPException(
            status_code=400, detail="'session_ids' must be a list of strings"
        )

    raw_known = body.get("known_revisions")
    known_revisions: dict[str, str | None] = {}
    if raw_known is not None:
        if not isinstance(raw_known, dict):
            raise HTTPException(
                status_code=400, detail="'known_revisions' must be an object"
            )
        for raw_session_id, raw_revision in raw_known.items():
            if not isinstance(raw_session_id, str):
                raise HTTPException(
                    status_code=400,
                    detail="'known_revisions' keys must be session ID strings",
                )
            session_ids = _parse_session_id_set([raw_session_id])
            if not session_ids:
                continue
            session_id = next(iter(session_ids))
            if raw_revision is not None and not isinstance(raw_revision, str):
                raise HTTPException(
                    status_code=400,
                    detail="'known_revisions' values must be strings or null",
                )
            known_revisions[session_id] = raw_revision

    if wanted is None and known_revisions:
        wanted = set(known_revisions.keys())

    limit = body.get("limit", 300)
    if not isinstance(limit, int) or limit < 1 or limit > 5000:
        raise HTTPException(
            status_code=400, detail="'limit' must be an integer between 1 and 5000"
        )

    rows = await asyncio.to_thread(scan_session_revisions, wanted)
    found_ids = {row["session_id"] for row in rows}

    changed: list[dict[str, str]] = []
    for row in rows:
        sid = row["session_id"]
        known = known_revisions.get(sid)
        if sid not in known_revisions or known != row["revision"]:
            changed.append(row)
            if len(changed) >= limit:
                break

    removed: list[str] = []
    if wanted is not None:
        removed = sorted(sid for sid in wanted if sid not in found_ids)[:limit]

    return {
        "changed": changed,
        "removed": removed,
    }


# TODO: sessions list only shows in-memory active sessions (current process).
# Sessions from previous server runs are on disk but not listed here.
# Future: union active sessions with disk-discovered session directories.
@router.get("/api/sessions", dependencies=[Depends(_require_api_key)])
async def list_sessions() -> dict:
    """List all active chat sessions with metadata."""
    from amplifier_distro.server.services import get_services

    try:
        services = get_services()
    except Exception:  # noqa: BLE001
        logger.warning("Services unavailable — returning empty session list")
        return {"sessions": []}

    sessions = services.backend.list_active_sessions()
    return {
        "sessions": [
            {
                "session_id": s.session_id,
                "working_dir": str(s.working_dir) if s.working_dir else None,
                "description": s.description,
                "is_active": s.is_active,
            }
            for s in sessions
        ]
    }


@router.get(
    "/api/sessions/{session_id}/transcript",
    dependencies=[Depends(_require_api_key)],
)
async def get_transcript(session_id: str) -> JSONResponse:
    """Return the transcript for a session as a JSON array of messages."""
    if not _VALID_SESSION_ID.fullmatch(session_id):
        return JSONResponse(
            status_code=400,
            content={"error": "Invalid session ID format"},
        )

    try:
        payload = await asyncio.to_thread(_load_transcript_payload, session_id)
    except OSError:
        logger.warning(
            "Failed to read transcript for session %r", session_id, exc_info=True
        )
        return JSONResponse(
            status_code=500,
            content={"error": "Failed to read transcript. Check server logs."},
        )

    if payload is None:
        return JSONResponse(
            status_code=404,
            content={"error": f"Session {session_id!r} not found"},
        )
    return JSONResponse(content=payload)


manifest = AppManifest(
    name="chat",
    description="Amplifier rich web chat interface with WebSocket streaming",
    version="0.1.0",
    router=router,
)
