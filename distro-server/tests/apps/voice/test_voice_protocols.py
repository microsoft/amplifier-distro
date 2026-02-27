"""Tests for voice protocol event streaming hook and voice display system.

Verifies EventStreamingHook maps Amplifier canonical events to SSE wire dicts.

Exit criteria (EventStreamingHook):
1. tool:pre maps to tool_call with status='pending'
2. tool:post maps to tool_result with output
3. content_block:start tracks block_type in _current_blocks
4. content_block:delta uses tracked block_type
5. content_block:end removes from _current_blocks
6. cancel:requested maps correctly with level
7. session:fork maps correctly with agent
8. large base64 data (>1000 chars) is stripped to '[image data omitted]'
9. small base64 data (<1000 chars) passes through unchanged

Exit criteria (VoiceDisplaySystem):
1. strips '=>' and '->'
2. strips '|' and '...'
3. truncates at sentence boundary at 200 chars, ends with '.'
4. adds 'Error:' prefix for error level
5. debug messages not spoken (should_speak=False)
6. suppressed pattern 'debug:' not spoken
7. normal info message is spoken
8. very short message ('ok', len<3) not spoken

Exit criteria (VoiceApprovalSystem):
1. safe tool (read_file) auto-approved
2. web_search auto-approved
3. dangerous tool (bash) pushes SSE approval_request event, returns True when approved
4. dangerous tool (write_file) returns False when denied
5. spoken prompt for bash includes command text
6. spoken prompt for write_file includes path
7. SAFE_TOOLS contains read_file, web_search, glob
8. DANGEROUS_TOOLS contains bash, write_file, git_push
"""

from __future__ import annotations

import asyncio

import pytest

from amplifier_distro.server.apps.voice.protocols.event_streaming import (
    EventStreamingHook,
)
from amplifier_distro.server.apps.voice.protocols.voice_approval import (
    VoiceApprovalSystem,
)
from amplifier_distro.server.apps.voice.protocols.voice_display import (
    VoiceDisplaySystem,
)


class TestEventStreamingHook:
    """Verify EventStreamingHook maps canonical events to SSE wire dicts."""

    def _make_hook(self) -> tuple[EventStreamingHook, asyncio.Queue]:
        queue: asyncio.Queue = asyncio.Queue()
        hook = EventStreamingHook(queue)
        return hook, queue

    # ------------------------------------------------------------------ #
    #  Tool Events                                                         #
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_tool_pre_maps_to_tool_call_with_pending_status(self) -> None:
        """tool:pre event maps to {type:'tool_call', status:'pending'}."""
        hook, queue = self._make_hook()
        data = {
            "tool_name": "read_file",
            "tool_call_id": "call_abc123",
            "arguments": {"path": "/tmp/test.txt"},
        }

        await hook("tool:pre", data)

        msg = queue.get_nowait()
        assert msg["type"] == "tool_call"
        assert msg["status"] == "pending"
        assert msg["tool_name"] == "read_file"
        assert msg["tool_call_id"] == "call_abc123"
        assert msg["arguments"] == {"path": "/tmp/test.txt"}

    @pytest.mark.asyncio
    async def test_tool_post_maps_to_tool_result_with_output(self) -> None:
        """tool:post event maps to {type:'tool_result', output, success}."""
        hook, queue = self._make_hook()
        data = {
            "tool_name": "read_file",
            "tool_call_id": "call_abc123",
            "output": "file contents here",
            "success": True,
            "error": None,
        }

        await hook("tool:post", data)

        msg = queue.get_nowait()
        assert msg["type"] == "tool_result"
        assert msg["tool_name"] == "read_file"
        assert msg["tool_call_id"] == "call_abc123"
        assert msg["output"] == "file contents here"
        assert msg["success"] is True

    # ------------------------------------------------------------------ #
    #  Content Block Events                                               #
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_content_block_start_tracks_block_type(self) -> None:
        """content_block:start maps to content_start and tracks block_type."""
        hook, queue = self._make_hook()
        data = {"block_type": "text", "index": 0}

        await hook("content_block:start", data)

        msg = queue.get_nowait()
        assert msg["type"] == "content_start"
        assert msg["block_type"] == "text"
        assert msg["index"] == 0
        # Block type is tracked internally
        assert hook._current_blocks[0] == "text"

    @pytest.mark.asyncio
    async def test_content_block_delta_uses_tracked_block_type(self) -> None:
        """content_block:delta uses block_type from _current_blocks."""
        hook, queue = self._make_hook()
        # First set up the block
        hook._current_blocks[1] = "text"

        data = {"index": 1, "delta": {"text": "Hello world"}}

        await hook("content_block:delta", data)

        msg = queue.get_nowait()
        assert msg["type"] == "content_delta"
        assert msg["index"] == 1
        assert msg["delta"] == "Hello world"
        assert msg["block_type"] == "text"

    @pytest.mark.asyncio
    async def test_content_block_end_removes_from_current_blocks(self) -> None:
        """content_block:end removes block from _current_blocks."""
        hook, queue = self._make_hook()
        # Set up block first
        hook._current_blocks[2] = "text"

        data = {"index": 2, "content": "Final content"}

        await hook("content_block:end", data)

        msg = queue.get_nowait()
        assert msg["type"] == "content_end"
        assert msg["index"] == 2
        assert msg["content"] == "Final content"
        assert msg["block_type"] == "text"
        # Block should be removed
        assert 2 not in hook._current_blocks

    # ------------------------------------------------------------------ #
    #  Cancel Events                                                       #
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_cancel_requested_maps_correctly_with_level(self) -> None:
        """cancel:requested maps to {type:'cancel_requested', level, running_tools}."""
        hook, queue = self._make_hook()
        data = {"level": "turn", "running_tools": ["read_file", "write_file"]}

        await hook("cancel:requested", data)

        msg = queue.get_nowait()
        assert msg["type"] == "cancel_requested"
        assert msg["level"] == "turn"
        assert msg["running_tools"] == ["read_file", "write_file"]

    # ------------------------------------------------------------------ #
    #  Session Events                                                      #
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_session_fork_maps_correctly_with_agent(self) -> None:
        """session:fork maps to {type:'session_fork', child_session_id, agent}."""
        hook, queue = self._make_hook()
        data = {"child_session_id": "child-session-xyz", "agent": "sub-agent-name"}

        await hook("session:fork", data)

        msg = queue.get_nowait()
        assert msg["type"] == "session_fork"
        assert msg["child_session_id"] == "child-session-xyz"
        assert msg["agent"] == "sub-agent-name"

    # ------------------------------------------------------------------ #
    #  Data Sanitization                                                   #
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_large_base64_data_stripped_to_placeholder(self) -> None:
        """Base64 data > 1000 chars is replaced with '[image data omitted]'."""
        hook, queue = self._make_hook()
        # Create a base64 string > 1000 chars
        large_base64 = "A" * 1001
        data = {
            "tool_name": "read_file",
            "tool_call_id": "call_xyz",
            "output": large_base64,
            "success": True,
            "error": None,
        }

        await hook("tool:post", data)

        msg = queue.get_nowait()
        assert msg["output"] == "[image data omitted]"

    @pytest.mark.asyncio
    async def test_small_base64_data_passes_through_unchanged(self) -> None:
        """Base64 data <= 1000 chars passes through unchanged."""
        hook, queue = self._make_hook()
        # Create a base64 string <= 1000 chars
        small_base64 = "A" * 999
        data = {
            "tool_name": "read_file",
            "tool_call_id": "call_xyz",
            "output": small_base64,
            "success": True,
            "error": None,
        }

        await hook("tool:post", data)

        msg = queue.get_nowait()
        assert msg["output"] == small_base64


class TestVoiceDisplaySystem:
    """Verify VoiceDisplaySystem formats messages for speech output."""

    # ------------------------------------------------------------------ #
    #  Text Formatting — stripping symbols                                #
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_strips_arrow_symbols(self) -> None:
        """spoken_text has '=>' and '->' stripped and whitespace collapsed."""
        system = VoiceDisplaySystem()
        msg = await system.display("Loading => result -> done", level="info")
        assert "=>" not in msg.spoken_text
        assert "->" not in msg.spoken_text
        assert "Loading" in msg.spoken_text
        assert "result" in msg.spoken_text
        assert "done" in msg.spoken_text

    @pytest.mark.asyncio
    async def test_strips_pipe_and_ellipsis(self) -> None:
        """spoken_text has '|' and '...' stripped and whitespace collapsed."""
        system = VoiceDisplaySystem()
        msg = await system.display("Step 1 | Step 2 ... Step 3", level="info")
        assert "|" not in msg.spoken_text
        assert "..." not in msg.spoken_text
        assert "Step 1" in msg.spoken_text
        assert "Step 2" in msg.spoken_text
        assert "Step 3" in msg.spoken_text

    # ------------------------------------------------------------------ #
    #  Text Formatting — truncation                                       #
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_truncates_at_sentence_boundary_within_200_chars(self) -> None:
        """Long messages are truncated at a sentence boundary and end with '.'."""
        system = VoiceDisplaySystem()
        # Build a message with 5 sentences of ~48 chars each (~250+ chars total)
        sentence = "This is a complete sentence for testing purposes"  # 48 chars
        long_msg = ". ".join([sentence] * 5) + "."
        assert len(long_msg) > 200

        msg = await system.display(long_msg, level="info")
        assert len(msg.spoken_text) <= 200
        assert msg.spoken_text.endswith(".")

    # ------------------------------------------------------------------ #
    #  Prefix injection                                                   #
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_adds_error_prefix_for_error_level(self) -> None:
        """spoken_text gets 'Error:' prefix when level=error and no error word present."""  # noqa: E501
        system = VoiceDisplaySystem()
        msg = await system.display("Something went wrong here", level="error")
        assert msg.spoken_text.startswith("Error:")

    # ------------------------------------------------------------------ #
    #  should_speak filtering                                             #
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_debug_messages_not_spoken(self) -> None:
        """Messages at DEBUG level have should_speak=False."""
        system = VoiceDisplaySystem()
        msg = await system.display("Some debug information here", level="debug")
        assert msg.should_speak is False

    @pytest.mark.asyncio
    async def test_suppressed_pattern_not_spoken(self) -> None:
        """Messages matching suppressed pattern 'debug:' have should_speak=False."""
        system = VoiceDisplaySystem()
        msg = await system.display("debug: internal trace info", level="info")
        assert msg.should_speak is False

    @pytest.mark.asyncio
    async def test_normal_info_message_is_spoken(self) -> None:
        """Normal info messages have should_speak=True."""
        system = VoiceDisplaySystem()
        msg = await system.display("Task completed successfully", level="info")
        assert msg.should_speak is True

    @pytest.mark.asyncio
    async def test_very_short_message_not_spoken(self) -> None:
        """Messages shorter than 3 chars (like 'ok') have should_speak=False."""
        system = VoiceDisplaySystem()
        msg = await system.display("ok", level="info")
        assert msg.should_speak is False


class TestVoiceApprovalSystem:
    """Verify VoiceApprovalSystem approves/denies tool calls with SSE events."""

    def _make_system(self) -> tuple[VoiceApprovalSystem, asyncio.Queue]:
        queue: asyncio.Queue = asyncio.Queue()
        system = VoiceApprovalSystem(queue)
        return system, queue

    # ------------------------------------------------------------------ #
    #  Auto-approval for safe tools                                       #
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_safe_tool_read_file_auto_approved(self) -> None:
        """Safe tool read_file is auto-approved without pushing any event."""
        system, queue = self._make_system()
        result = await system.request_approval("read_file", {"path": "/tmp/test.txt"})
        assert result is True
        assert queue.empty()

    @pytest.mark.asyncio
    async def test_safe_tool_web_search_auto_approved(self) -> None:
        """Safe tool web_search is auto-approved without pushing any event."""
        system, queue = self._make_system()
        result = await system.request_approval("web_search", {"query": "python"})
        assert result is True
        assert queue.empty()

    # ------------------------------------------------------------------ #
    #  Dangerous tools require approval                                   #
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_dangerous_tool_bash_pushes_event_and_returns_true_when_approved(
        self,
    ) -> None:
        """bash pushes approval_request SSE event, returns True when approved."""
        system, queue = self._make_system()

        task = asyncio.create_task(
            system.request_approval("bash", {"command": "ls -la /tmp"})
        )

        # Yield to event loop so the task runs up to its await point
        await asyncio.sleep(0)

        event = queue.get_nowait()
        assert event["type"] == "approval_request"
        assert event["tool_name"] == "bash"
        assert event["is_dangerous"] is True
        assert "request_id" in event
        assert "spoken_prompt" in event

        # Approve and collect result
        system.handle_response(True)
        result = await task
        assert result is True

    @pytest.mark.asyncio
    async def test_dangerous_tool_write_file_returns_false_when_denied(
        self,
    ) -> None:
        """write_file pushes approval_request SSE event, returns False when denied."""
        system, queue = self._make_system()

        task = asyncio.create_task(
            system.request_approval("write_file", {"path": "/tmp/output.txt"})
        )

        await asyncio.sleep(0)

        event = queue.get_nowait()
        assert event["type"] == "approval_request"
        assert event["tool_name"] == "write_file"

        system.handle_response(False)
        result = await task
        assert result is False

    # ------------------------------------------------------------------ #
    #  Spoken prompt generation                                           #
    # ------------------------------------------------------------------ #

    def test_spoken_prompt_bash_includes_command_text(self) -> None:
        """Spoken prompt for bash contains the command being executed."""
        system, _ = self._make_system()
        prompt = system.generate_spoken_prompt("bash", {"command": "rm -rf /tmp/old"})
        assert "rm -rf /tmp/old" in prompt

    def test_spoken_prompt_write_file_includes_path(self) -> None:
        """Spoken prompt for write_file contains the target path."""
        system, _ = self._make_system()
        prompt = system.generate_spoken_prompt(
            "write_file", {"path": "/etc/config.yml"}
        )
        assert "/etc/config.yml" in prompt

    # ------------------------------------------------------------------ #
    #  Class variable membership                                          #
    # ------------------------------------------------------------------ #

    def test_safe_tools_contains_expected_members(self) -> None:
        """SAFE_TOOLS contains read_file, web_search, and glob."""
        assert "read_file" in VoiceApprovalSystem.SAFE_TOOLS
        assert "web_search" in VoiceApprovalSystem.SAFE_TOOLS
        assert "glob" in VoiceApprovalSystem.SAFE_TOOLS

    def test_dangerous_tools_contains_expected_members(self) -> None:
        """DANGEROUS_TOOLS contains bash, write_file, and git_push."""
        assert "bash" in VoiceApprovalSystem.DANGEROUS_TOOLS
        assert "write_file" in VoiceApprovalSystem.DANGEROUS_TOOLS
        assert "git_push" in VoiceApprovalSystem.DANGEROUS_TOOLS
