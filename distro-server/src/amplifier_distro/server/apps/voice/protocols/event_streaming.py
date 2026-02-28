"""Voice event streaming hook.

Maps Amplifier canonical events to SSE wire dicts for voice streaming clients.
Handles 24 canonical events and sanitizes large base64 payloads.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
from typing import Any

try:
    from amplifier_core.models import HookResult as _HookResult  # type: ignore[import]

    _HOOK_RESULT_AVAILABLE = True
except ImportError:
    _HookResult = None  # type: ignore[assignment]
    _HOOK_RESULT_AVAILABLE = False


# 24 canonical Amplifier events to capture for voice streaming
EVENTS_TO_CAPTURE: list[str] = [
    "content_block:start",
    "content_block:delta",
    "content_block:end",
    "thinking:delta",
    "thinking:final",
    "tool:pre",
    "tool:post",
    "tool:error",
    "session:fork",
    "session:start",
    "session:end",
    "session:resume",  # handled by fallback → session_resume
    "provider:request",
    "llm:request",
    "llm:request:raw",
    "provider:response",
    "llm:response",
    "llm:response:raw",
    "context:compaction",
    "user:notification",
    "cancel:requested",
    "cancel:completed",
    "orchestrator:complete",  # handled by fallback → orchestrator_complete
    "prompt:submit",  # handled by fallback → prompt_submit
]

_BASE64_PLACEHOLDER = "[image data omitted]"
_BASE64_LENGTH_THRESHOLD = 1000


class EventStreamingHook:
    """Hook that maps Amplifier canonical events to SSE wire dicts.

    Pushes mapped messages to an async queue for SSE consumption.
    Sanitizes large base64 payloads to keep streaming payloads small.
    """

    name = "voice-event-streaming"
    priority = 100

    def __init__(self, event_queue: asyncio.Queue) -> None:
        self._queue = event_queue
        # Tracks block_index -> block_type for content blocks in progress
        self._current_blocks: dict[int, str] = {}

    async def __call__(self, event: str, data: dict[str, Any]) -> Any:
        """Process an event and push SSE wire dict to queue if mappable."""
        sanitized = self._sanitize_for_streaming(data)
        message = self._map_event_to_message(event, sanitized)
        if message is not None:
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(message)

        if _HOOK_RESULT_AVAILABLE and _HookResult is not None:
            return _HookResult(action="continue")
        return None

    def _sanitize_for_streaming(self, data: dict[str, Any]) -> dict[str, Any]:
        """Strip large base64 strings from data to keep payloads manageable.

        Recursively scans dict values and replaces any string longer than
        1000 characters with '[image data omitted]'.

        Returns a shallow copy of data with large strings replaced.
        """
        result = copy.copy(data)
        for key, value in result.items():
            if isinstance(value, str) and len(value) > _BASE64_LENGTH_THRESHOLD:
                result[key] = _BASE64_PLACEHOLDER
            elif isinstance(value, dict):
                result[key] = self._sanitize_for_streaming(value)
        return result

    def _map_event_to_message(
        self, event: str, data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Map an Amplifier canonical event to an SSE wire dict.

        Returns None for events that should not be forwarded.
        """
        if event == "content_block:start":
            block_type = data.get("block_type", "text")
            index = data.get("index", 0)
            self._current_blocks[index] = block_type
            return {"type": "content_start", "block_type": block_type, "index": index}

        if event == "content_block:delta":
            index = data.get("index", 0)
            delta_dict = data.get("delta", {})
            # Extract text from delta dict
            delta_text = (
                delta_dict.get("text", "")
                if isinstance(delta_dict, dict)
                else delta_dict
            )
            block_type = self._current_blocks.get(index, "text")
            return {
                "type": "content_delta",
                "index": index,
                "delta": delta_text,
                "block_type": block_type,
            }

        if event == "content_block:end":
            index = data.get("index", 0)
            block_type = self._current_blocks.pop(index, "text")
            return {
                "type": "content_end",
                "index": index,
                "content": data.get("content"),
                "block_type": block_type,
            }

        if event == "thinking:delta":
            return {"type": "thinking_delta"}

        if event == "thinking:final":
            return {"type": "thinking_final"}

        if event == "tool:pre":
            return {
                "type": "tool_call",
                "tool_name": data.get("tool_name"),
                "tool_call_id": data.get("tool_call_id"),
                "arguments": data.get("arguments"),
                "status": "pending",
            }

        if event == "tool:post":
            return {
                "type": "tool_result",
                "tool_name": data.get("tool_name"),
                "tool_call_id": data.get("tool_call_id"),
                "output": data.get("output"),
                "success": data.get("success"),
                "error": data.get("error"),
            }

        if event == "tool:error":
            return {"type": "tool_error"}

        if event == "session:fork":
            return {
                "type": "session_fork",
                "child_session_id": data.get("child_session_id"),
                "agent": data.get("agent"),
            }

        if event == "session:start":
            return {"type": "session_start"}

        if event == "session:end":
            return {"type": "session_end"}

        if event in ("provider:request", "llm:request", "llm:request:raw"):
            return {"type": "provider_request", "event": event}

        if event in ("provider:response", "llm:response", "llm:response:raw"):
            return {"type": "provider_response", "event": event}

        if event == "context:compaction":
            return {"type": "context_compaction"}

        if event == "user:notification":
            return {"type": "display_message"}

        if event == "cancel:requested":
            return {
                "type": "cancel_requested",
                "level": data.get("level"),
                "running_tools": data.get("running_tools"),
            }

        if event == "cancel:completed":
            return {
                "type": "cancel_completed",
                "level": data.get("level"),
                "tools_cancelled": data.get("tools_cancelled"),
            }

        # Unknown event: derive type name from event string
        type_name = event.replace(":", "_").replace("_block", "")
        return {"type": type_name, "event": event}
