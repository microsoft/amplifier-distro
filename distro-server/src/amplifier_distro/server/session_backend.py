"""Server-level session backend - shared across all apps.

The SessionBackend protocol defines how any server app (chat, Slack,
voice, etc.) creates and interacts with Amplifier sessions. The server
owns ONE backend instance and shares it with all apps.

Implementations:
- MockBackend: Echo/canned responses (testing, dev/simulator mode)
- FoundationBackend: Real sessions via amplifier-foundation (production)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from amplifier_distro.conventions import AMPLIFIER_HOME, PROJECTS_DIR
from amplifier_distro.features import AMPLIFIER_START_URI
from amplifier_distro.metadata_persistence import register_metadata_hooks
from amplifier_distro.server.spawn_registration import register_spawning
from amplifier_distro.transcript_persistence import register_transcript_hooks

logger = logging.getLogger(__name__)


@dataclass
class SessionInfo:
    """Information about a backend session."""

    session_id: str
    project_id: str = ""
    working_dir: str = ""
    is_active: bool = True
    # Which app created this session (e.g., "chat", "slack", "voice")
    created_by_app: str = ""
    description: str = ""


@dataclass
class _SessionHandle:
    """Lightweight handle wrapping a foundation session.

    Keeps the metadata the backend needs without coupling to bridge
    internals.  The ``session`` field holds the actual
    ``AmplifierSession`` object from amplifier-foundation.
    """

    session_id: str
    project_id: str
    working_dir: Path
    session: Any  # AmplifierSession from foundation
    _cleanup_done: bool = field(default=False, repr=False)
    prepared: Any = field(default=None, repr=False)
    hook_unregister: Callable[[], None] | None = field(default=None, repr=False)

    async def run(self, prompt: str) -> str:
        """Execute a prompt and return the response text."""
        if self.session is None:
            raise RuntimeError(
                f"Session {self.session_id} has no active session object"
            )
        return await self.session.execute(prompt)

    async def cleanup(self) -> None:
        """Clean up the session resources."""
        if self._cleanup_done or self.session is None:
            return
        try:
            await self.session.cleanup()
        except Exception:  # noqa: BLE001
            logger.debug("Session cleanup error for %s", self.session_id, exc_info=True)
        finally:
            self._cleanup_done = True

    async def cancel(self, level: str = "graceful") -> None:
        """Request cancellation of the running session."""
        if self.session is None:
            return
        coordinator = getattr(self.session, "coordinator", None)
        if coordinator is None:
            return
        request_cancel = getattr(coordinator, "request_cancel", None)
        if request_cancel is not None:
            try:
                if asyncio.iscoroutinefunction(request_cancel):
                    await request_cancel(level)
                else:
                    request_cancel(level)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Error requesting cancel (level=%s)", level, exc_info=True
                )


@runtime_checkable
class SessionBackend(Protocol):
    """Protocol for Amplifier session interaction.

    This is the contract that all session backends implement.
    Apps get a backend instance from ServerServices and use it
    to create, message, and manage sessions.
    """

    async def create_session(
        self,
        working_dir: str = "~",
        bundle_name: str | None = None,
        description: str = "",
        event_queue: asyncio.Queue | None = None,
    ) -> SessionInfo:
        """Create a new Amplifier session. Returns session info."""
        ...

    async def send_message(self, session_id: str, message: str) -> str:
        """Send a message to a session. Returns the response text."""
        ...

    async def end_session(self, session_id: str) -> None:
        """End a session and clean up."""
        ...

    async def resume_session(
        self,
        session_id: str,
        working_dir: str,
        event_queue: asyncio.Queue | None = None,
    ) -> None:
        """Restore LLM transcript context for a previously created session."""
        ...

    async def execute(
        self, session_id: str, prompt: str, images: list[str] | None = None
    ) -> None:
        """Execute a prompt in a session. Events stream via event_queue."""
        ...

    async def cancel_session(self, session_id: str, level: str = "graceful") -> None:
        """Request cancellation of a running session."""
        ...

    def resolve_approval(self, session_id: str, request_id: str, choice: str) -> bool:
        """Resolve a pending approval request. Returns True if resolved."""
        ...

    async def get_session_info(self, session_id: str) -> SessionInfo | None:
        """Get info about an active session."""
        ...

    def get_session_config(self, session_id: str) -> dict[str, Any] | None:
        """Get the mount-plan config dict for an active session."""
        ...

    def list_active_sessions(self) -> list[SessionInfo]:
        """List all active sessions managed by this backend."""
        ...


class MockBackend:
    """Mock backend for testing and simulation.

    Returns echo responses or configurable canned responses.
    Tracks all interactions for test assertions.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, SessionInfo] = {}
        self._session_counter: int = 0
        self._message_history: dict[str, list[dict[str, str]]] = {}
        # Configurable response handler
        self._response_fn: Any = None
        # Recorded calls for test assertions
        self.calls: list[dict[str, Any]] = []

    def set_response_fn(self, fn: Any) -> None:
        """Set a custom response function: (session_id, message) -> response."""
        self._response_fn = fn

    async def create_session(
        self,
        working_dir: str = "~",
        bundle_name: str | None = None,
        description: str = "",
        event_queue: Any = None,
    ) -> SessionInfo:
        self._session_counter += 1
        session_id = f"mock-session-{self._session_counter:04d}"
        info = SessionInfo(
            session_id=session_id,
            project_id=f"mock-project-{self._session_counter}",
            working_dir=working_dir,
            is_active=True,
            description=description,
        )
        self._sessions[session_id] = info
        self._message_history[session_id] = []
        self.calls.append(
            {
                "method": "create_session",
                "working_dir": working_dir,
                "bundle_name": bundle_name,
                "description": description,
                "result": session_id,
            }
        )
        return info

    async def send_message(self, session_id: str, message: str) -> str:
        info = self._sessions.get(session_id)
        if info is None or not info.is_active:
            raise ValueError(f"Unknown session: {session_id}")

        self._message_history[session_id].append({"role": "user", "content": message})
        self.calls.append(
            {
                "method": "send_message",
                "session_id": session_id,
                "message": message,
            }
        )

        # Use custom response function if set
        if self._response_fn:
            response = self._response_fn(session_id, message)
        else:
            response = f"[Mock response to: {message}]"

        self._message_history[session_id].append(
            {"role": "assistant", "content": response}
        )
        return response

    async def end_session(self, session_id: str) -> None:
        if session_id in self._sessions:
            self._sessions[session_id].is_active = False
        self.calls.append({"method": "end_session", "session_id": session_id})

    async def get_session_info(self, session_id: str) -> SessionInfo | None:
        return self._sessions.get(session_id)

    def get_session_config(self, session_id: str) -> dict[str, Any] | None:
        return None  # MockBackend has no real session config

    def list_active_sessions(self) -> list[SessionInfo]:
        return [s for s in self._sessions.values() if s.is_active]

    def get_message_history(self, session_id: str) -> list[dict[str, str]]:
        """Get the full message history for a session (testing helper)."""
        return self._message_history.get(session_id, [])

    async def resume_session(
        self, session_id: str, working_dir: str, event_queue: Any = None
    ) -> None:
        """No-op resume for testing. Records the call for assertion."""
        self.calls.append(
            {
                "method": "resume_session",
                "session_id": session_id,
                "working_dir": working_dir,
            }
        )

    async def execute(
        self, session_id: str, prompt: str, images: list[str] | None = None
    ) -> None:
        """No-op execute for testing. Records the call."""
        self.calls.append(
            {
                "method": "execute",
                "session_id": session_id,
                "prompt": prompt,
                "images": images,
            }
        )

    async def cancel_session(self, session_id: str, level: str = "graceful") -> None:
        """No-op cancel for testing. Records the call."""
        self.calls.append(
            {
                "method": "cancel_session",
                "session_id": session_id,
                "level": level,
            }
        )

    def resolve_approval(self, session_id: str, request_id: str, choice: str) -> bool:
        """No-op resolve for testing. Always returns False."""
        self.calls.append(
            {
                "method": "resolve_approval",
                "session_id": session_id,
                "request_id": request_id,
                "choice": choice,
            }
        )
        return False


class FoundationBackend:
    """Real backend using amplifier-foundation for Amplifier sessions.

    This connects to the Amplifier runtime via foundation's bundle loading
    and session creation APIs.  Used in production mode.

    NOTE: Requires amplifier-foundation to be available at runtime.
    """

    def __init__(self, bundle_name: str = AMPLIFIER_START_URI) -> None:
        self._bundle_name = bundle_name
        self._sessions: dict[str, _SessionHandle] = {}
        self._reconnect_locks: dict[str, asyncio.Lock] = {}
        # Per-session FIFO queues for serializing handle.run() calls
        self._session_queues: dict[str, asyncio.Queue] = {}
        # Worker tasks draining each session queue
        self._worker_tasks: dict[str, asyncio.Task] = {}
        # Tombstone: sessions that were intentionally ended (blocks reconnect)
        self._ended_sessions: set[str] = set()
        self._approval_systems: dict[str, Any] = {}
        # Guard: sessions whose hooks have already been wired (prevents
        # double-registration on page refresh / resume)
        self._wired_sessions: set[str] = set()

    async def _load_bundle(self, bundle_name: str | None = None) -> Any:
        """Load and prepare a bundle via foundation.

        If a local overlay bundle exists (created by the install wizard),
        loads it by path.  The overlay includes the maintained distro bundle and any
        user-selected features, so everything composes automatically.
        Falls back to loading the bundle by name if no overlay exists.
        """
        from amplifier_foundation import load_bundle

        from amplifier_distro.overlay import overlay_dir, overlay_exists

        if overlay_exists():
            bundle = await load_bundle(str(overlay_dir()))
        else:
            name = bundle_name or self._bundle_name
            bundle = await load_bundle(name)
        return await bundle.prepare()

    def _wire_event_queue(
        self, session: Any, session_id: str, event_queue: asyncio.Queue
    ) -> Callable[[], None]:
        """Wire streaming, display, and approval closures to an event queue.

        Called from create_session() and resume_session() when event_queue
        is provided. All three closures push (event_name, data) tuples
        to the same queue.

        Returns an unregister callable that removes all registered hooks and
        removes the session from _wired_sessions, so the next call (on
        reconnect) re-registers fresh hooks against the new queue.

        Guards against double hook registration on page refresh / resume:
        hooks are only registered once per session; subsequent calls update
        only the approval system (which needs the new queue).
        """
        from amplifier_distro.server.protocol_adapters import (
            ApprovalSystem,
            QueueDisplaySystem,
        )

        _q = event_queue
        coordinator = session.coordinator

        if session_id in self._wired_sessions:
            # Already wired — update approval system only (new queue connection).
            # Don't re-register hooks; return the existing unregister callable
            # so the caller can still clean up when it's done.
            def _on_approval_request_rewire(
                request_id: str,
                prompt: str,
                options: list[str],
                timeout: float,
                default: str,
            ) -> None:
                try:
                    _q.put_nowait(
                        (
                            "approval_request",
                            {
                                "request_id": request_id,
                                "prompt": prompt,
                                "options": options,
                                "timeout": timeout,
                                "default": default,
                            },
                        )
                    )
                except asyncio.QueueFull:
                    logger.warning("Event queue full, dropping approval_request")

            approval = ApprovalSystem(
                on_approval_request=_on_approval_request_rewire,
                auto_approve=False,
            )
            if hasattr(coordinator, "set"):
                coordinator.set("approval", approval)
            self._approval_systems[session_id] = approval
            existing = self._sessions.get(session_id)
            if existing and existing.hook_unregister:
                return existing.hook_unregister
            return lambda: None

        self._wired_sessions.add(session_id)

        # 1. Streaming hook — all coordinator events to queue.
        # The hooks API requires an async handler returning HookResult.
        # There is no wildcard support — register for each event explicitly.
        from amplifier_core.events import ALL_EVENTS
        from amplifier_core.models import HookResult

        async def on_stream(event: str, data: dict) -> HookResult:
            try:
                _q.put_nowait((event, data))
            except asyncio.QueueFull:
                logger.warning("Event queue full, dropping event: %s", event)
            return HookResult(action="continue", data=data)

        hooks = coordinator.hooks
        _registered_hooks: list[tuple[str, Any]] = []

        registered = 0
        failed_evts = []
        for evt in ALL_EVENTS:
            try:
                hooks.register(evt, on_stream)
                _registered_hooks.append((evt, on_stream))
                registered += 1
            except Exception as exc:  # noqa: BLE001
                failed_evts.append((evt, exc))

        # Delegate events are not in ALL_EVENTS — register explicitly
        for evt in [
            "delegate:agent_spawned",
            "delegate:agent_resumed",
            "delegate:agent_completed",
            "delegate:error",
        ]:
            try:
                hooks.register(evt, on_stream)
                _registered_hooks.append((evt, on_stream))
                registered += 1
            except Exception as exc:  # noqa: BLE001
                failed_evts.append((evt, exc))

        # Bridge hook — re-emit orchestrator:complete as prompt:complete.
        # hooks-session-naming (and any foundation hook) listens to
        # prompt:complete, but loop-streaming emits orchestrator:complete.
        # Inject session_id since it's absent from the orchestrator event data.
        async def on_orchestrator_complete(event: str, data: dict) -> HookResult:
            await coordinator.hooks.emit(
                "prompt:complete", {**data, "session_id": session_id}
            )
            return HookResult(action="continue", data=data)

        try:
            hooks.register(
                "orchestrator:complete", on_orchestrator_complete, priority=50
            )
            _registered_hooks.append(
                ("orchestrator:complete", on_orchestrator_complete)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to register prompt:complete bridge: %s", exc)

        logger.info(
            "Event hook wiring: %d registered, %d failed for session %s",
            registered,
            len(failed_evts),
            session_id,
        )
        if failed_evts:
            for evt, exc in failed_evts:
                logger.warning("  hook registration failed [%s]: %s", evt, exc)

        # 2. Display system — display messages to queue
        display = QueueDisplaySystem(event_queue)
        if hasattr(coordinator, "set"):
            coordinator.set("display", display)

        # 3. Approval system — approval requests to queue
        def _on_approval_request(
            request_id: str,
            prompt: str,
            options: list[str],
            timeout: float,
            default: str,
        ) -> None:
            try:
                _q.put_nowait(
                    (
                        "approval_request",
                        {
                            "request_id": request_id,
                            "prompt": prompt,
                            "options": options,
                            "timeout": timeout,
                            "default": default,
                        },
                    )
                )
            except asyncio.QueueFull:
                logger.warning("Event queue full, dropping approval_request")

        approval = ApprovalSystem(
            on_approval_request=_on_approval_request,
            auto_approve=False,
        )
        if hasattr(coordinator, "set"):
            coordinator.set("approval", approval)
        self._approval_systems[session_id] = approval

        # Build and return the unregister callable.  Calling it removes all
        # registered hooks from the coordinator and evicts the session from
        # _wired_sessions so the next _wire_event_queue call re-registers
        # fresh hooks against the new queue (critical for correct reconnect).
        def _unregister() -> None:
            for evt, fn in _registered_hooks:
                with contextlib.suppress(Exception):
                    hooks.unregister(evt, fn)
            self._wired_sessions.discard(session_id)

        return _unregister

    async def create_session(
        self,
        working_dir: str = "~",
        bundle_name: str | None = None,
        description: str = "",
        event_queue: asyncio.Queue | None = None,
        exclude_tools: list[str] | None = None,
    ) -> SessionInfo:
        wd = Path(working_dir).expanduser()

        prepared = await self._load_bundle(bundle_name)
        session = await prepared.create_session(session_cwd=wd)

        session_id = session.session_id
        # Foundation derives project_id by replacing path separators with "-"
        # (e.g. /Users/sam/repo -> -Users-sam-repo).  AmplifierSession does not
        # expose project_id directly, so we derive it the same way.
        project_id = str(wd).replace("/", "-")
        handle = _SessionHandle(
            session_id=session_id,
            project_id=project_id,
            working_dir=wd,
            session=session,
            prepared=prepared,
        )
        self._sessions[session_id] = handle

        # Wire persistence hooks
        session_dir = (
            Path(AMPLIFIER_HOME).expanduser()
            / PROJECTS_DIR
            / handle.project_id
            / "sessions"
            / session_id
        )
        register_transcript_hooks(session, session_dir)

        # Metadata is written on the first orchestrator:complete (not
        # eagerly) so we don't create the session dir before the transcript
        # hook does — that would confuse the frontend revision poll.
        from datetime import UTC, datetime

        register_metadata_hooks(
            session,
            session_dir,
            initial_metadata={
                "session_id": session_id,
                "created": datetime.now(tz=UTC).isoformat(),
                "bundle": bundle_name or self._bundle_name,
                "working_dir": str(wd),
                "description": description,
            },
        )

        # Wire streaming/display/approval when event_queue provided
        if event_queue is not None:
            handle.hook_unregister = self._wire_event_queue(
                session, session_id, event_queue
            )

        # Register session spawning capability
        register_spawning(session, prepared, session_id, exclude_tools=exclude_tools)

        # Pre-start the session worker so the first message doesn't pay
        # the task-creation overhead
        queue: asyncio.Queue = asyncio.Queue()
        self._session_queues[session_id] = queue
        self._worker_tasks[session_id] = asyncio.create_task(
            self._session_worker(session_id)
        )

        return SessionInfo(
            session_id=session_id,
            project_id=handle.project_id,
            working_dir=str(handle.working_dir),
            is_active=True,
            description=description,
        )

    async def send_message(self, session_id: str, message: str) -> str:
        handle = self._sessions.get(session_id)
        if handle is None:
            # Session handle lost (server restart). Use per-session lock
            # to prevent concurrent reconnects for the same session_id.
            lock = self._reconnect_locks.setdefault(session_id, asyncio.Lock())
            try:
                async with lock:
                    # Double-check: another coroutine may have reconnected
                    # while we waited for the lock.
                    handle = self._sessions.get(session_id)
                    if handle is None:
                        handle = await self._reconnect(session_id)
            finally:
                # Clean up lock entry on both success and failure paths
                self._reconnect_locks.pop(session_id, None)

        # Route through the per-session queue so concurrent calls serialize
        if session_id not in self._session_queues:
            self._session_queues[session_id] = asyncio.Queue()
        if (
            session_id not in self._worker_tasks
            or self._worker_tasks[session_id].done()
        ):
            self._worker_tasks[session_id] = asyncio.create_task(
                self._session_worker(session_id)
            )

        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        await self._session_queues[session_id].put((message, future))
        return await future

    async def execute(
        self, session_id: str, prompt: str, images: list[str] | None = None
    ) -> None:
        """Execute a prompt with streaming via the event queue.

        Unlike send_message() which routes through the worker queue and
        returns response text, execute() calls handle.run() directly.
        Events stream via the event_queue wired at create_session() time.
        """
        handle = self._sessions.get(session_id)
        if handle is None:
            raise ValueError(f"Unknown session: {session_id}")
        # TODO: wire images to handle.run() when image attachment support is added
        await handle.run(prompt)

    async def cancel_session(self, session_id: str, level: str = "graceful") -> None:
        """Cancel a running session. No-op for unknown IDs."""
        handle = self._sessions.get(session_id)
        if handle is None:
            return
        await handle.cancel(level)

    def resolve_approval(self, session_id: str, request_id: str, choice: str) -> bool:
        """Unblock a pending approval gate. Sync — not async.

        Returns True if the request was found and resolved.
        """
        approval = self._approval_systems.get(session_id)
        if approval is None:
            return False
        return approval.handle_response(request_id, choice)

    def _find_transcript(self, session_id: str) -> list[dict[str, Any]]:
        """Find and load a session transcript from disk.

        Scans all project directories under ``~/.amplifier/projects/`` for
        a transcript matching *session_id*.  Returns the parsed message list.
        Raises :class:`FileNotFoundError` if no transcript is found.
        """
        projects_dir = Path(AMPLIFIER_HOME).expanduser() / PROJECTS_DIR
        if not projects_dir.exists():
            raise FileNotFoundError(f"Projects directory not found: {projects_dir}")

        for project_dir in projects_dir.iterdir():
            if not project_dir.is_dir():
                continue
            transcript_path = project_dir / "sessions" / session_id / "transcript.jsonl"
            if transcript_path.exists():
                messages: list[dict[str, Any]] = []
                for line in transcript_path.read_text().splitlines():
                    line = line.strip()
                    if line:
                        messages.append(json.loads(line))
                return messages

        raise FileNotFoundError(f"No transcript found for session {session_id}")

    async def _reconnect(
        self, session_id: str, *, working_dir: str = "~"
    ) -> _SessionHandle:
        """Resume a session whose handle was lost (e.g. after server restart).

        Loads the transcript from disk, adds synthetic results for any
        orphaned tool calls, creates a fresh session with
        ``is_resumed=True``, and injects the transcript into the context.
        On success the handle is cached so subsequent messages don't pay
        the resume cost again.
        """
        if session_id in self._ended_sessions:
            raise ValueError(
                f"Session {session_id} was intentionally ended"
                " and cannot be reconnected"
            )
        logger.info("Attempting to reconnect session %s", session_id)
        try:
            # 1. Load transcript from disk
            transcript = self._find_transcript(session_id)

            # 2. Handle orphaned tool calls (tool_use without matching result)
            from amplifier_foundation.session import (
                add_synthetic_tool_results,
                find_orphaned_tool_calls,
            )

            orphan_ids = find_orphaned_tool_calls(transcript)
            if orphan_ids:
                transcript = add_synthetic_tool_results(transcript, orphan_ids)
                logger.info(
                    "Added synthetic results for %d orphaned tool calls in %s",
                    len(orphan_ids),
                    session_id,
                )

            # 3. Create a fresh session with the same bundle
            wd = Path(working_dir).expanduser()
            # CWD safety guard: BundleRegistry.__init__ calls os.getcwd() which
            # raises FileNotFoundError if the process CWD was deleted (e.g. in
            # container or temp-dir environments). Silently recover by moving to ~.
            try:
                os.getcwd()
            except FileNotFoundError:
                os.chdir(os.path.expanduser("~"))
            prepared = await self._load_bundle()
            session = await prepared.create_session(
                session_id=session_id,
                session_cwd=wd,
                is_resumed=True,
            )

            # 4. Inject transcript (preserve fresh system prompt from create)
            context = session.coordinator.get("context")
            if context and hasattr(context, "set_messages"):
                current_msgs = await context.get_messages()
                system_msgs = [m for m in current_msgs if m.get("role") == "system"]

                await context.set_messages(transcript)

                # Re-inject system prompt if transcript lacks one
                restored = await context.get_messages()
                if system_msgs and not any(m.get("role") == "system" for m in restored):
                    await context.set_messages(system_msgs + restored)

            # 5. Build handle and worker infrastructure
            # Derive project_id the same way foundation does (path separators → "-")
            project_id = str(wd).replace("/", "-")
            handle = _SessionHandle(
                session_id=session_id,
                project_id=project_id,
                working_dir=wd,
                session=session,
                prepared=prepared,
            )
            self._sessions[session_id] = handle

            # Wire persistence hooks on reconnect too
            session_dir = (
                Path(AMPLIFIER_HOME).expanduser()
                / PROJECTS_DIR
                / handle.project_id
                / "sessions"
                / session_id
            )
            register_transcript_hooks(session, session_dir)
            register_metadata_hooks(session, session_dir)

            # Register session spawning capability on reconnect
            register_spawning(session, prepared, session_id)

            queue: asyncio.Queue = asyncio.Queue()
            self._session_queues[session_id] = queue
            self._worker_tasks[session_id] = asyncio.create_task(
                self._session_worker(session_id)
            )

            logger.info(
                "Session %s reconnected (%d messages restored)",
                session_id,
                len(transcript),
            )
            return handle

        except Exception as err:
            logger.warning("Failed to reconnect session %s", session_id, exc_info=True)
            raise ValueError(f"Unknown session: {session_id}") from err

    async def _session_worker(self, session_id: str) -> None:
        """Drain the session queue, running handle.run() calls sequentially.

        Receives (message, future) tuples from the queue.  A ``None``
        sentinel signals the worker to exit cleanly (used by end_session
        and stop).  On CancelledError, drains remaining futures with
        cancellation so callers don't wait forever.
        """
        queue = self._session_queues[session_id]
        while True:
            try:
                item = await queue.get()
            except asyncio.CancelledError:
                # Drain remaining items and cancel their futures
                while not queue.empty():
                    try:
                        pending_item = queue.get_nowait()
                        if pending_item is not None:
                            _, fut = pending_item
                            if not fut.done():
                                fut.cancel()
                        queue.task_done()
                    except asyncio.QueueEmpty:
                        break
                raise

            if item is None:
                # Sentinel -- exit cleanly
                queue.task_done()
                break

            message, future = item
            try:
                handle = self._sessions.get(session_id)
                if handle is None:
                    future.set_exception(
                        ValueError(f"Session {session_id} handle not found")
                    )
                else:
                    result = await handle.run(message)
                    if not future.done():
                        future.set_result(result)
            except asyncio.CancelledError:
                if not future.done():
                    future.cancel()
                raise
            except Exception as exc:  # noqa: BLE001
                if not future.done():
                    future.set_exception(exc)
            finally:
                queue.task_done()

    async def end_session(self, session_id: str) -> None:
        # Tombstone first -- prevents _reconnect() from reviving this session
        self._ended_sessions.add(session_id)
        self._wired_sessions.discard(session_id)
        self._approval_systems.pop(session_id, None)

        # Pop handle before signalling the worker
        handle = self._sessions.pop(session_id, None)

        # Signal worker to exit cleanly via sentinel
        queue = self._session_queues.get(session_id)
        if queue is not None:
            await queue.put(None)

        # Wait up to 5 s for in-flight work to drain
        worker = self._worker_tasks.get(session_id)
        if worker is not None and not worker.done():
            try:
                await asyncio.wait_for(asyncio.shield(worker), timeout=5.0)
            except TimeoutError:
                logger.warning(
                    "Session worker %s did not drain in 5s, cancelling", session_id
                )
                worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await worker

        # Drain any remaining queued futures
        if queue is not None:
            while not queue.empty():
                try:
                    item = queue.get_nowait()
                    if item is not None:
                        _, fut = item
                        if not fut.done():
                            fut.cancel()
                except asyncio.QueueEmpty:
                    break

        # Clean up references
        self._session_queues.pop(session_id, None)
        self._worker_tasks.pop(session_id, None)

        if handle:
            await handle.cleanup()

    async def stop(self) -> None:
        """Gracefully stop all session workers.

        Sends the None sentinel to every active queue, then waits up to
        10 s for workers to drain.  Remaining workers are cancelled.
        Must be called during server shutdown.
        """
        for queue in list(self._session_queues.values()):
            await queue.put(None)

        if self._worker_tasks:
            workers = [t for t in self._worker_tasks.values() if not t.done()]
            if workers:
                _, still_pending = await asyncio.wait(workers, timeout=10.0)
                for task in still_pending:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

        self._session_queues.clear()
        self._worker_tasks.clear()
        self._approval_systems.clear()
        self._wired_sessions.clear()

    async def get_session_info(self, session_id: str) -> SessionInfo | None:
        handle = self._sessions.get(session_id)
        if handle is None:
            return None
        return SessionInfo(
            session_id=handle.session_id,
            project_id=handle.project_id,
            working_dir=str(handle.working_dir),
        )

    def get_hook_unregister(self, session_id: str) -> Callable[[], None] | None:
        """Return the hook unregister callable for a session, if one was wired.

        Called by VoiceConnection.create() so it can store the callable and
        invoke it in _cleanup_hook() on disconnect, ensuring hooks are properly
        removed before the next reconnect re-registers fresh ones.
        """
        handle = self._sessions.get(session_id)
        return handle.hook_unregister if handle else None

    def get_session_config(self, session_id: str) -> dict[str, Any] | None:
        """Return the mount-plan config dict for a live session."""
        handle = self._sessions.get(session_id)
        if handle is None or handle.session is None:
            return None
        return getattr(handle.session, "config", None)

    def list_active_sessions(self) -> list[SessionInfo]:
        return [
            SessionInfo(
                session_id=h.session_id,
                project_id=h.project_id,
                working_dir=str(h.working_dir),
            )
            for h in self._sessions.values()
        ]

    async def resume_session(
        self,
        session_id: str,
        working_dir: str,
        event_queue: asyncio.Queue | None = None,
    ) -> None:
        """Restore the LLM context for a session after a server restart."""
        if event_queue is not None:
            self._ended_sessions.discard(session_id)

        if self._sessions.get(session_id) is None:
            await self._reconnect(session_id, working_dir=working_dir)

        if event_queue is not None:
            handle = self._sessions.get(session_id)
            if handle is not None:
                handle.hook_unregister = self._wire_event_queue(
                    handle.session, session_id, event_queue
                )

            # Ensure worker queue + task exist after resume
            if session_id not in self._session_queues:
                self._session_queues[session_id] = asyncio.Queue()
            if (
                session_id not in self._worker_tasks
                or self._worker_tasks[session_id].done()
            ):
                self._worker_tasks[session_id] = asyncio.create_task(
                    self._session_worker(session_id)
                )
