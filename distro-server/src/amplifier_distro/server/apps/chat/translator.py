"""SessionEventTranslator — maps kernel events to wire protocol.

Kernel events arrive as (event_name, data) tuples from the asyncio.Queue.
Translator maps them to wire protocol dicts for the WebSocket client.

State maintained across a turn:
  - _cycle_count: increments on each tool:post (handles server index resets)
  - _block_map: {f"{cycle}-{server_index}" -> local_index} for stable DOM ids
  - local_index_counter: monotonically increasing across the full turn
  - _pending_delegates: deque of tool_call_ids for delegate/task correlations

State reset on orchestrator:complete (start of next turn).
"""

from __future__ import annotations

from collections import deque
from typing import Any


class SessionEventTranslator:
    """Translates raw kernel events to wire protocol messages."""

    def __init__(self) -> None:
        self._cycle_count: int = 0
        self._block_map: dict[str, int] = {}
        self._local_index_counter: int = 0
        self._pending_delegates: deque[str] = deque()

    def get_local_index(self, server_index: int) -> int:
        """Map (cycle, server_index) composite key to a stable local index.

        Uses max(server_index, counter) so that cycle-0 blocks pass through
        unchanged (local == server), while post-cycle blocks always get indices
        beyond any previously assigned ones.
        """
        key = f"{self._cycle_count}-{server_index}"
        if key not in self._block_map:
            local = max(server_index, self._local_index_counter)
            self._block_map[key] = local
            self._local_index_counter = local + 1
        return self._block_map[key]

    def reset(self) -> None:
        """Clear per-turn state on prompt_complete."""
        self._cycle_count = 0
        self._block_map = {}
        self._local_index_counter = 0
        self._pending_delegates.clear()

    @staticmethod
    def lineage_fields(data: dict[str, Any]) -> dict[str, str]:
        """Extract lineage metadata for UI provenance labels.

        Returns session_id and parent_id when present and non-empty.
        """
        out: dict[str, str] = {}
        session_id = data.get("session_id")
        if isinstance(session_id, str) and session_id:
            out["session_id"] = session_id
        parent_id = data.get("parent_id")
        if isinstance(parent_id, str) and parent_id:
            out["parent_id"] = parent_id
        return out

    @staticmethod
    def server_index(data: dict[str, Any]) -> int:
        """Extract block index from runtime payloads (new and legacy shapes)."""
        raw = data.get("block_index", data.get("index"))
        if raw is None:
            block = data.get("block")
            if isinstance(block, dict):
                raw = block.get("block_index", block.get("index"))
            elif block is not None:
                raw = getattr(block, "block_index", getattr(block, "index", None))
        if raw is None:
            raw = 0
        if isinstance(raw, int):
            return raw
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def block_text(data: dict[str, Any]) -> str:
        """Extract text from content block payloads (new and legacy shapes)."""
        block = data.get("block")
        if isinstance(block, dict):
            for key in ("text", "thinking", "content", "delta"):
                value = block.get(key)
                if isinstance(value, str):
                    return value
        elif block is not None:
            for attr in ("text", "thinking", "content"):
                value = getattr(block, attr, None)
                if isinstance(value, str):
                    return value
        text = data.get("text", "")
        if isinstance(text, str):
            return text
        content = data.get("content")
        return content if isinstance(content, str) else ""

    def translate(self, event_name: str, data: dict[str, Any]) -> dict[str, Any] | None:
        """Translate a kernel event to a wire protocol dict.

        Returns None for events that should be silently skipped.
        """
        match event_name:
            case "content_block:start":
                return {
                    "type": "content_start",
                    "block_type": data.get("block_type", "text"),
                    "index": self.get_local_index(self.server_index(data)),
                }

            case "content_block:delta":
                delta = data.get("delta")
                if isinstance(delta, dict):
                    # Anthropic native format: {"type": "text_delta", "text": "..."}
                    delta = delta.get("text") or delta.get("thinking") or ""
                if not isinstance(delta, str):
                    delta = data.get("text", "")
                if not isinstance(delta, str):
                    delta = ""
                return {
                    "type": "content_delta",
                    "delta": delta,
                    "index": self.get_local_index(self.server_index(data)),
                }

            case "content_block:end":
                return {
                    "type": "content_end",
                    "index": self.get_local_index(self.server_index(data)),
                    "text": self.block_text(data),
                }

            case "thinking:delta":
                return {
                    "type": "thinking_delta",
                    "delta": data.get("delta", ""),
                }

            case "thinking:final":
                return {
                    "type": "thinking_final",
                    "content": data.get("content", ""),
                }

            case "tool:pre":
                tool_name = data.get("tool_name", "")
                tool_call_id = data.get("tool_call_id", "")
                if tool_name in ("delegate", "task"):
                    self._pending_delegates.append(tool_call_id)
                return {
                    "type": "tool_call",
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "arguments": data.get("tool_input", {}),
                    **self.lineage_fields(data),
                }

            case "tool:post":
                result = data.get("result")
                output = None
                error = None
                # result is None when the kernel fires tool:post before result
                # is available (not expected in normal flow; treat as success)
                success = True
                if result is not None:
                    output = str(result.output) if result.output is not None else None
                    error = result.error if hasattr(result, "error") else None
                    success = error is None
                self._cycle_count += 1
                return {
                    "type": "tool_result",
                    "tool_call_id": data.get("tool_call_id", ""),
                    "success": success,
                    "output": output,
                    "error": error,
                    **self.lineage_fields(data),
                }

            case "tool:error":
                return {
                    "type": "tool_result",
                    "tool_call_id": data.get("tool_call_id", ""),
                    "success": False,
                    "output": None,
                    "error": data.get("error", "Unknown error"),
                    **self.lineage_fields(data),
                }

            case "delegate:agent_spawned":
                parent_tool_call_id = (
                    self._pending_delegates.popleft()
                    if self._pending_delegates
                    else None
                )
                return {
                    "type": "session_fork",
                    "parent_id": data.get("parent_id", ""),
                    "child_id": data.get("child_id", ""),
                    "agent": data.get("agent", ""),
                    "parent_tool_call_id": parent_tool_call_id,
                }

            case "orchestrator:complete":
                self.reset()
                return {
                    "type": "prompt_complete",
                    "turn_count": data.get("turn_count", 0),
                }

            case "cancel:completed":
                return {"type": "execution_cancelled"}

            case "cancel:requested":
                return {"type": "cancel_acknowledged"}

            case "display_message":
                return {
                    "type": "display_message",
                    "message": data.get("message", ""),
                    "level": data.get("level", "info"),
                    "source": data.get("source", "system"),
                }

            case "approval_request":
                return {
                    "type": "approval_request",
                    "id": data.get("request_id", ""),
                    "prompt": data.get("prompt", ""),
                    "options": data.get("options", []),
                    "timeout": data.get("timeout", 300),
                    "default": data.get("default", "deny"),
                }

            case "llm:response" | "provider:post":
                # Token usage from each LLM call. Multiple may fire per turn
                # (e.g. tool loop), so frontend accumulates across the turn.
                usage = data.get("usage") or {}
                input_t = usage.get("input_tokens", 0)
                output_t = usage.get("output_tokens", 0)
                return {
                    "type": "token_usage",
                    "input_tokens": input_t,
                    "output_tokens": output_t,
                    "total_tokens": usage.get("total_tokens", input_t + output_t),
                    "cache_read_tokens": usage.get("cache_read_tokens"),
                    "cache_write_tokens": usage.get("cache_write_tokens"),
                    "model": data.get("model"),
                    "provider": data.get("provider"),
                    "duration_ms": data.get("duration_ms"),
                }

            case _:
                return None
