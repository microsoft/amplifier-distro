"""Voice approval system for tool call authorization.

Manages approval of tool calls by classifying them as safe (auto-approved)
or dangerous (requiring explicit user approval via SSE event).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, ClassVar


class VoiceApprovalSystem:
    """Approves or denies tool calls based on their risk classification.

    Safe tools are auto-approved. Dangerous tools push an SSE
    approval_request event to the queue and await a handle_response call.

    Concurrency contract: only one approval may be in flight at a time.
    Tool calls execute sequentially in the voice session loop, so this is
    guaranteed by the caller. An assertion in request_approval enforces the
    contract and will surface any future misuse immediately.
    """

    SAFE_TOOLS: ClassVar[set[str]] = {
        "read_file",
        "web_search",
        "web_fetch",
        "git_log",
        "git_status",
        "glob",
        "grep",
        "list_directory",
        "LSP",
        "python_check",
        "filesystem_read_file",
        "filesystem_list_directory",
        "fetch",
        "search",
        "git_diff",
        "git_show",
    }

    DANGEROUS_TOOLS: ClassVar[set[str]] = {
        "bash",
        "write_file",
        "edit_file",
        "delete_file",
        "apply_patch",
        "git_push",
        "git_commit",
        "git_reset",
        "git_checkout",
        "filesystem_write_file",
        "filesystem_delete",
        "move_file",
    }

    # Keywords that suggest destructive intent for unknown tools
    _DANGEROUS_KEYWORDS: frozenset[str] = frozenset(
        {"write", "delete", "push", "commit", "reset", "checkout", "patch", "move"}
    )

    def __init__(self, event_queue: asyncio.Queue) -> None:
        self._queue = event_queue
        self._pending_event: asyncio.Event | None = None
        self._pending_result: bool = False

    async def request_approval(self, tool_name: str, arguments: dict[str, Any]) -> bool:
        """Return True if the tool call is approved, False if denied.

        Safe tools are approved immediately. Dangerous tools (or unknown
        tools matching dangerous patterns) push an SSE approval_request
        event and wait for handle_response to be called.
        """
        if tool_name in self.SAFE_TOOLS:
            return True

        is_dangerous = (
            tool_name in self.DANGEROUS_TOOLS
            or self._matches_dangerous_pattern(tool_name)
        )

        if is_dangerous:
            assert self._pending_event is None, (
                "VoiceApprovalSystem does not support concurrent approvals; "
                "a previous request_approval call is still pending"
            )
            request_id = str(uuid.uuid4())
            spoken_prompt = self.generate_spoken_prompt(tool_name, arguments)
            event: dict[str, Any] = {
                "type": "approval_request",
                "request_id": request_id,
                "tool_name": tool_name,
                "spoken_prompt": spoken_prompt,
                "is_dangerous": True,
            }
            self._pending_event = asyncio.Event()
            self._pending_result = False
            await self._queue.put(event)
            await self._pending_event.wait()
            self._pending_event = None  # Reset so the next approval can proceed
            return self._pending_result

        # Unknown tool with no dangerous pattern: auto-approve
        return True

    def _matches_dangerous_pattern(self, tool_name: str) -> bool:
        """Return True if an unknown tool name contains dangerous keywords."""
        lower = tool_name.lower()
        return any(kw in lower for kw in self._DANGEROUS_KEYWORDS)

    def handle_response(self, approved: bool) -> None:
        """Set the approval result and unblock the pending request_approval call."""
        self._pending_result = approved
        if self._pending_event is not None:
            self._pending_event.set()

    def generate_spoken_prompt(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Generate a natural-language spoken prompt for the given tool call."""
        lower = tool_name.lower()

        if "bash" in lower or "execute" in lower:
            cmd = str(arguments.get("command", arguments.get("cmd", "")))
            return f"I need to run: {cmd[:60]}. Shall I proceed?"

        if "write" in lower:
            path = str(arguments.get("path", arguments.get("file_path", "")))
            return f"May I write to {path}?"

        if "delete" in lower:
            path = str(arguments.get("path", arguments.get("file_path", "")))
            return f"May I delete {path}?"

        if tool_name == "git_push":
            return "May I push to the remote repository?"

        if tool_name == "git_commit":
            return "May I create a git commit?"

        return f"May I use {tool_name}?"
