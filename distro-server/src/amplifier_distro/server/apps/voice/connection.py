"""VoiceConnection — manages one voice session lifecycle.

One instance per voice connection. Owns:
  - event_queue: asyncio.Queue wired to EventStreamingHook for SSE streaming
  - _hook: EventStreamingHook that maps Amplifier events to SSE wire dicts
  - _hook_unregister: Callable to unregister the hook on teardown/end

HOOK CLEANUP: Critical — without unregistering in finally, dead hook registrations
accumulate across reconnects and fire against closed queues.

SPAWN CAPABILITY: Critical — without registering spawn, delegate tool sub-sessions
bypass shared backend entirely (no hooks, no observability, no session tracking).
Always register before first handle.run().
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from amplifier_distro.server.apps.voice.protocols.event_streaming import (
    EventStreamingHook,
)
from amplifier_distro.server.protocol_adapters import web_chat_surface

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

    @property
    def event_queue(self) -> asyncio.Queue:
        """The asyncio.Queue used as the event bus for this connection."""
        return self._event_queue

    @property
    def session_id(self) -> str | None:
        """The current Amplifier session ID, or None if not yet created."""
        return self._session_id

    async def create(self, workspace_root: str) -> str:
        """Create an Amplifier session for this voice connection.

        1. Creates EventStreamingHook wired to the event queue
        2. Calls backend.create_session(description='voice', working_dir=...,
           surface=web_chat_surface(self._event_queue)) — hook wiring happens
           automatically inside create_session when surface is passed
        3. Stores session_id and session_obj
        4. Registers 'spawn' capability on session.coordinator so delegate tool
           sub-sessions use the shared backend (hooks, observability, tracking)
        5. Returns session_id
        """
        # 1. Create the streaming hook wired to our event queue
        hook = EventStreamingHook(event_queue=self._event_queue)
        self._hook = hook

        # 2. Create session via backend — surface wires the hook internally
        session = await self._backend.create_session(
            description="voice",
            working_dir=workspace_root,
            surface=web_chat_surface(self._event_queue),
        )

        # 3. Store session references
        self._session_obj = session
        self._session_id = session.session_id

        # 4. Register 'spawn' capability so delegate tool sub-sessions route through
        #    shared backend (ensures hooks, observability, and session tracking)
        coordinator = getattr(session, "coordinator", None)
        if coordinator is not None:
            register_capability = getattr(coordinator, "register_capability", None)
            if register_capability is not None:
                register_capability("spawn", self._spawn_child_session)

        assert self._session_id is not None  # set above from session.session_id
        return self._session_id

    async def _spawn_child_session(self, **kwargs: Any) -> Any:
        """Spawn a child session through the backend with the same event queue.

        Called when the 'spawn' capability is invoked by the delegate tool.
        Ensures child sessions use the shared backend (hooks, observability, tracking).
        Without this, delegate tool sub-sessions bypass the backend entirely.
        """
        return await self._backend.create_session(
            surface=web_chat_surface(self._event_queue),
            **kwargs,
        )

    async def teardown(self) -> None:
        """Handle client disconnect: mark session disconnected, always unregister hook.

        Critical: _hook_unregister() is called unconditionally in finally to prevent
        dead hook accumulation across reconnects that could fire against closed queues.
        """
        try:
            if self._session_id is not None:
                await self._backend.mark_disconnected(self._session_id)
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

    async def cancel(self, immediate: bool = False) -> None:
        """Cancel the running session."""
        if self._session_id is not None:
            await self._backend.cancel_session(
                self._session_id, level="immediate" if immediate else "graceful"
            )

    def _cleanup_hook(self) -> None:
        """Unregister the hook if one is registered. Always safe to call."""
        if self._hook_unregister is not None:
            try:
                self._hook_unregister()
            except Exception:  # noqa: BLE001
                logger.warning("Error unregistering voice event hook", exc_info=True)
            finally:
                self._hook_unregister = None
