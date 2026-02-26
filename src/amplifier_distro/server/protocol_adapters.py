"""Protocol adapters for server-side event streaming.

Self-contained implementations of the approval and display protocols
for use with FoundationBackend's event queue wiring. No bridge, no
foundation imports — pure asyncio utilities.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from collections.abc import Callable
from typing import Any, Literal

logger = logging.getLogger(__name__)


class ApprovalSystem:
    """Interactive approval system using asyncio.Event for WebSocket integration.

    In auto_approve mode: immediately returns first option (headless usage).
    In interactive mode: blocks request_approval() until handle_response()
    is called from another coroutine (e.g. the WebSocket receive loop).

    on_approval_request: callback(request_id, prompt, options, timeout, default)
      Called when a new approval request is pending — use this to notify
      the WebSocket client that approval is needed.
    """

    def __init__(
        self,
        on_approval_request: Callable[..., Any] | None = None,
        auto_approve: bool = True,
    ) -> None:
        self._on_approval_request = on_approval_request
        self._auto_approve = auto_approve
        self._pending: dict[str, asyncio.Event] = {}
        self._responses: dict[str, str] = {}

    async def request_approval(
        self,
        prompt: str,
        options: list[str],
        timeout: float = 300.0,
        default: str = "deny",
    ) -> str:
        if self._auto_approve:
            return options[0] if options else "allow"

        request_id = str(uuid.uuid4())
        event = asyncio.Event()
        self._pending[request_id] = event

        try:
            if self._on_approval_request:
                result = self._on_approval_request(
                    request_id, prompt, options, timeout, default
                )
                if inspect.isawaitable(result):
                    await result
            await asyncio.wait_for(event.wait(), timeout=timeout)
            return self._responses.pop(request_id, default)
        except TimeoutError:
            return default
        finally:
            self._pending.pop(request_id, None)
            self._responses.pop(request_id, None)

    def handle_response(self, request_id: str, choice: str) -> bool:
        """Unblock a waiting request_approval().

        Returns True if found and not already resolved.
        """
        event = self._pending.get(request_id)
        if event is None:
            return False
        if event.is_set():
            return False
        self._responses[request_id] = choice
        event.set()
        return True


class QueueDisplaySystem:
    """Display system that pushes messages to an asyncio.Queue.

    Satisfies the display protocol expected by the coordinator.
    Every show_message() call enqueues a ("display_message", {...}) tuple.
    """

    def __init__(
        self,
        queue: asyncio.Queue,
        nesting_depth: int = 0,  # type: ignore[type-arg]
    ) -> None:
        self._queue = queue
        self._nesting_depth = nesting_depth

    async def show_message(
        self,
        message: str,
        level: Literal["info", "warning", "error"] = "info",
        source: str = "hook",
    ) -> None:
        self._queue.put_nowait(
            (
                "display_message",
                {"message": message, "level": level, "source": source},
            )
        )

    def push_nesting(self) -> QueueDisplaySystem:
        return QueueDisplaySystem(self._queue, self._nesting_depth + 1)

    def pop_nesting(self) -> QueueDisplaySystem:
        return QueueDisplaySystem(self._queue, max(0, self._nesting_depth - 1))

    @property
    def nesting_depth(self) -> int:
        return self._nesting_depth
