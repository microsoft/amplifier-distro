"""VoiceConnection — manages one voice session lifecycle.

One instance per voice connection. Owns:
  - event_queue: asyncio.Queue wired to EventStreamingHook for SSE streaming
  - _hook: EventStreamingHook that maps Amplifier events to SSE wire dicts
  - _hook_unregister: Callable to unregister the hook on teardown/end

HOOK CLEANUP: Critical — without unregistering in finally, dead hook registrations
accumulate across reconnects and fire against closed queues.

SPAWN CAPABILITY: Handled by FoundationBackend via spawn_registration.py — registers
session.spawn on the coordinator at create_session() time.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from amplifier_distro.server.apps.voice.protocols.event_streaming import (
    EventStreamingHook,
)

logger = logging.getLogger(__name__)

# Maximum event queue depth — bounds memory if SSE consumer is slow
_EVENT_QUEUE_MAX_SIZE = 10000


class VoiceConnection:
    """Manages one voice session lifecycle: create, teardown, end, cancel."""

    def __init__(self, repository: Any, backend: Any) -> None:
        self._repository = repository
        self._backend = backend
        self._event_queue: asyncio.Queue = asyncio.Queue(maxsize=_EVENT_QUEUE_MAX_SIZE)
        self._hook: EventStreamingHook | None = None
        self._hook_unregister: Callable[[], None] | None = None
        self._session_id: str | None = None
        self._session_obj: Any = None
        self._project_id: str | None = None

    @property
    def event_queue(self) -> asyncio.Queue:
        """The asyncio.Queue used as the event bus for this connection."""
        return self._event_queue

    @property
    def session_id(self) -> str | None:
        """The current Amplifier session ID, or None if not yet created."""
        return self._session_id

    @property
    def project_id(self) -> str | None:
        """The Amplifier project ID for this session, or None if not yet created.

        Used to locate the Amplifier transcript path:
        ~/.amplifier/projects/{project_id}/sessions/{session_id}/transcript.jsonl
        """
        return self._project_id

    async def create(self, workspace_root: str) -> str:
        """Create an Amplifier session for this voice connection.

        1. Creates EventStreamingHook wired to the event queue
        2. Calls backend.create_session(description='voice', working_dir=...,
           event_queue=...) — hook wiring happens automatically inside create_session
           when event_queue is passed
        3. Stores session_id and session_obj
        4. Registers 'spawn' capability on session.coordinator so delegate tool
           sub-sessions use the shared backend (hooks, observability, tracking)
        5. Returns session_id
        """
        # 1. Create the streaming hook wired to our event queue
        hook = EventStreamingHook(event_queue=self._event_queue)
        self._hook = hook

        # 2. Create session via backend — event_queue wires the hook internally.
        # exclude_tools=["delegate"] enforces the pure-orchestrator model: the
        # voice model decides what to delegate; sub-agents must not re-delegate
        # back through the voice bridge (would create recursive loops).
        session = await self._backend.create_session(
            description="voice",
            working_dir=workspace_root,
            event_queue=self._event_queue,
            exclude_tools=["delegate"],
        )

        # 3. Store session references
        self._session_obj = session
        self._session_id = session.session_id
        self._project_id = getattr(session, "project_id", None)

        # Fallback: if the backend doesn't expose project_id on the session object,
        # scan the Amplifier projects directory to find which project owns this
        # session. Without project_id, write_to_amplifier_transcript is never called
        # and voice sessions remain invisible to the Amplifier chat app.
        if self._project_id is None and self._session_id is not None:
            self._project_id = self._find_project_id_from_fs(self._session_id)
            if self._project_id is None:
                logger.warning(
                    "project_id not available for voice session %s; "
                    "voice sessions will not appear in the Amplifier chat app. "
                    "Ensure the backend returns project_id on the session object.",
                    self._session_id,
                )

        assert self._session_id is not None  # set above from session.session_id

        # Fetch and store the hook unregister callable so _cleanup_hook() can
        # remove the registered hooks on disconnect. Without this, hooks from
        # the previous connection remain and fire against the stale queue.
        get_unregister = getattr(self._backend, "get_hook_unregister", None)
        if get_unregister is not None:
            self._hook_unregister = get_unregister(self._session_id)

        return self._session_id

    async def teardown(self) -> None:
        """Handle client disconnect: mark session disconnected, always cleanup hook.

        Critical: _cleanup_hook() is called unconditionally in finally to prevent
        dead hook accumulation across reconnects that could fire against closed queues.
        """
        try:
            if self._session_id is not None:
                self._repository.update_status(self._session_id, "disconnected")
        finally:
            self._cleanup_hook()
            # Reset queue so reconnect gets a fresh event bus
            self._event_queue = asyncio.Queue(maxsize=_EVENT_QUEUE_MAX_SIZE)

    async def end(self, reason: str = "user_ended") -> None:
        """End the session permanently.

        Critical: _hook_unregister() called in finally to prevent dead hooks.
        """
        try:
            if self._session_id is not None:
                await self._backend.end_session(self._session_id)
                self._repository.end_conversation(self._session_id, reason)
        finally:
            self._cleanup_hook()

    async def cancel(self, level: str = "graceful") -> None:
        """Cancel the running session."""
        if self._session_id is not None:
            await self._backend.cancel_session(self._session_id, level=level)

    def _cleanup_hook(self) -> None:
        """Unregister the hook if one is registered. Always safe to call."""
        if self._hook_unregister is not None:
            try:
                self._hook_unregister()
            except Exception:  # noqa: BLE001
                logger.warning("Error unregistering voice event hook", exc_info=True)
            finally:
                self._hook_unregister = None

    @staticmethod
    def _find_project_id_from_fs(session_id: str) -> str | None:
        """Scan ~/.amplifier/projects/ to find the project owning this session.

        Called when the backend session object lacks a project_id attribute.
        Iterates project directories looking for a sessions/{session_id} subdir.
        Returns the project directory name (which is the project_id), or None.
        """
        from pathlib import Path

        projects_dir = Path.home() / ".amplifier" / "projects"
        if not projects_dir.exists():
            return None
        try:
            for project_dir in projects_dir.iterdir():
                if not project_dir.is_dir():
                    continue
                if (project_dir / "sessions" / session_id).exists():
                    return project_dir.name
        except OSError:
            logger.debug("Error scanning projects dir for project_id", exc_info=True)
        return None
