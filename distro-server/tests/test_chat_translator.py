"""Tests for SessionEventTranslator."""

from __future__ import annotations

from amplifier_distro.server.apps.chat.translator import SessionEventTranslator


class TestBasicTranslations:
    def setup_method(self):
        self.t = SessionEventTranslator()

    def test_content_block_start_text(self):
        msg = self.t.translate(
            "content_block:start", {"block_type": "text", "index": 0}
        )
        assert msg == {"type": "content_start", "block_type": "text", "index": 0}

    def test_content_block_start_uses_block_index(self):
        msg = self.t.translate(
            "content_block:start", {"block_type": "text", "block_index": 1}
        )
        assert msg == {"type": "content_start", "block_type": "text", "index": 1}

    def test_content_block_start_thinking(self):
        msg = self.t.translate(
            "content_block:start", {"block_type": "thinking", "index": 1}
        )
        assert msg == {"type": "content_start", "block_type": "thinking", "index": 1}

    def test_content_block_delta(self):
        msg = self.t.translate("content_block:delta", {"delta": "hello", "index": 0})
        assert msg == {"type": "content_delta", "delta": "hello", "index": 0}

    def test_content_block_delta_anthropic_text_delta_dict(self):
        # Regression: kernel passes Anthropic native format where delta is a dict,
        # not a plain string. Text content was silently dropped (delta set to "").
        msg = self.t.translate(
            "content_block:delta",
            {"delta": {"type": "text_delta", "text": "The answer is 42"}, "index": 0},
        )
        assert msg == {"type": "content_delta", "delta": "The answer is 42", "index": 0}

    def test_content_block_delta_anthropic_thinking_delta_dict(self):
        # Regression: thinking deltas via content_block:delta
        # (dict form) were also dropped.
        msg = self.t.translate(
            "content_block:delta",
            {
                "delta": {"type": "thinking_delta", "thinking": "I should consider..."},
                "index": 1,
            },
        )
        assert msg == {
            "type": "content_delta",
            "delta": "I should consider...",
            "index": 1,
        }

    def test_content_block_end(self):
        msg = self.t.translate("content_block:end", {"index": 0})
        assert msg == {"type": "content_end", "index": 0, "text": ""}

    def test_content_block_end_extracts_nested_block_text(self):
        msg = self.t.translate(
            "content_block:end",
            {
                "block_index": 1,
                "block": {"type": "text", "text": "hello"},
            },
        )
        assert msg == {"type": "content_end", "index": 1, "text": "hello"}

    def test_content_block_end_extracts_object_block_text(self):
        block = type("B", (), {"thinking": "object-thinking"})()
        msg = self.t.translate(
            "content_block:end",
            {
                "block_index": 2,
                "block": block,
            },
        )
        assert msg == {"type": "content_end", "index": 2, "text": "object-thinking"}

    def test_content_block_end_uses_index_from_block_object(self):
        block = type("B", (), {"index": 3, "text": "x"})()
        msg = self.t.translate("content_block:end", {"block": block})
        assert msg["index"] == 3

    def test_thinking_delta(self):
        msg = self.t.translate("thinking:delta", {"delta": "I think..."})
        assert msg == {"type": "thinking_delta", "delta": "I think..."}

    def test_thinking_final(self):
        msg = self.t.translate("thinking:final", {"content": "My conclusion"})
        assert msg == {"type": "thinking_final", "content": "My conclusion"}

    def test_tool_pre(self):
        msg = self.t.translate(
            "tool:pre",
            {
                "tool_call_id": "tc-001",
                "tool_name": "read_file",
                "tool_input": {"file_path": "/tmp/foo.py"},
            },
        )
        assert msg == {
            "type": "tool_call",
            "tool_call_id": "tc-001",
            "tool_name": "read_file",
            "arguments": {"file_path": "/tmp/foo.py"},
        }

    def test_tool_post_success(self):
        result = type("R", (), {"output": "file contents", "error": None})()
        msg = self.t.translate(
            "tool:post",
            {
                "tool_call_id": "tc-001",
                "result": result,
            },
        )
        assert msg == {
            "type": "tool_result",
            "tool_call_id": "tc-001",
            "success": True,
            "output": "file contents",
            "error": None,
        }

    def test_tool_post_error(self):
        result = type("R", (), {"output": None, "error": "File not found"})()
        msg = self.t.translate(
            "tool:post",
            {
                "tool_call_id": "tc-001",
                "result": result,
            },
        )
        assert msg == {
            "type": "tool_result",
            "tool_call_id": "tc-001",
            "success": False,
            "output": None,
            "error": "File not found",
        }

    def test_tool_pre_passes_lineage_fields(self):
        msg = self.t.translate(
            "tool:pre",
            {
                "tool_call_id": "tc-002",
                "tool_name": "grep",
                "tool_input": {"pattern": "TODO"},
                "session_id": "sess-child",
                "parent_id": "sess-parent",
            },
        )
        assert msg["session_id"] == "sess-child"
        assert msg["parent_id"] == "sess-parent"

    def test_tool_post_passes_lineage_fields(self):
        result = type("R", (), {"output": "ok", "error": None})()
        msg = self.t.translate(
            "tool:post",
            {
                "tool_call_id": "tc-003",
                "result": result,
                "session_id": "sess-child",
                "parent_id": "sess-parent",
            },
        )
        assert msg["session_id"] == "sess-child"
        assert msg["parent_id"] == "sess-parent"

    def test_tool_post_dict_shape_success(self):
        # Regression: kernel serializes ToolResult to dict via .model_dump()
        # before emitting tool:post. Handler must handle both dict and object shapes.
        msg = self.t.translate(
            "tool:post",
            {
                "tool_call_id": "tc-001",
                "result": {"output": "file contents", "error": None},
            },
        )
        assert msg == {
            "type": "tool_result",
            "tool_call_id": "tc-001",
            "success": True,
            "output": "file contents",
            "error": None,
        }

    def test_tool_post_dict_shape_error(self):
        # Regression: dict shape with error field must be handled defensively.
        msg = self.t.translate(
            "tool:post",
            {
                "tool_call_id": "tc-001",
                "result": {"output": None, "error": "File not found"},
            },
        )
        assert msg == {
            "type": "tool_result",
            "tool_call_id": "tc-001",
            "success": False,
            "output": None,
            "error": "File not found",
        }

    def test_tool_error(self):
        msg = self.t.translate(
            "tool:error",
            {
                "tool_call_id": "tc-002",
                "error": "Timeout",
            },
        )
        assert msg == {
            "type": "tool_result",
            "tool_call_id": "tc-002",
            "success": False,
            "error": "Timeout",
            "output": None,
        }

    def test_orchestrator_complete(self):
        msg = self.t.translate("orchestrator:complete", {"turn_count": 3})
        assert msg == {"type": "prompt_complete", "turn_count": 3}

    def test_cancel_completed(self):
        msg = self.t.translate("cancel:completed", {})
        assert msg == {"type": "execution_cancelled"}

    def test_cancel_requested(self):
        msg = self.t.translate("cancel:requested", {})
        assert msg == {"type": "cancel_acknowledged"}

    def test_unknown_event_returns_none(self):
        msg = self.t.translate("some:unknown:event", {"data": "value"})
        assert msg is None

    def test_display_message(self):
        msg = self.t.translate(
            "display_message",
            {
                "message": "Loading tools...",
                "level": "info",
                "source": "hook",
            },
        )
        assert msg == {
            "type": "display_message",
            "message": "Loading tools...",
            "level": "info",
            "source": "hook",
        }


class TestCycleAndIndexRemapping:
    """Block index resets to 0 after each tool call.

    Translator remaps to stable index.
    """

    def setup_method(self):
        self.t = SessionEventTranslator()

    def test_first_text_block_gets_index_0(self):
        msg = self.t.translate(
            "content_block:start", {"block_type": "text", "index": 0}
        )
        assert msg["index"] == 0

    def test_index_remapped_after_tool_result(self):
        self.t.translate("content_block:start", {"block_type": "text", "index": 0})
        result = type("R", (), {"output": "ok", "error": None})()
        self.t.translate("tool:post", {"tool_call_id": "tc-001", "result": result})
        msg = self.t.translate(
            "content_block:start", {"block_type": "text", "index": 0}
        )
        # After one block (index 0) and one tool:post, counter is at 1.
        # Next cycle's server_index=0 must remap to local index 1.
        assert msg["index"] == 1

    def test_cycle_count_increments_on_tool_post(self):
        result = type("R", (), {"output": "ok", "error": None})()
        assert self.t._cycle_count == 0
        self.t.translate("tool:post", {"tool_call_id": "tc-001", "result": result})
        assert self.t._cycle_count == 1
        self.t.translate("tool:post", {"tool_call_id": "tc-002", "result": result})
        assert self.t._cycle_count == 2

    def test_block_map_cleared_on_prompt_complete(self):
        self.t.translate("content_block:start", {"block_type": "text", "index": 0})
        assert len(self.t._block_map) > 0
        self.t.translate("orchestrator:complete", {"turn_count": 1})
        assert len(self.t._block_map) == 0
        assert self.t._cycle_count == 0

    def test_local_index_stable_across_cycles(self):
        """Each block in each cycle gets a unique, monotonically increasing index."""
        m0 = self.t.translate("content_block:start", {"block_type": "text", "index": 0})
        result = type("R", (), {"output": "ok", "error": None})()
        self.t.translate("tool:post", {"tool_call_id": "tc-001", "result": result})
        m1 = self.t.translate("content_block:start", {"block_type": "text", "index": 0})
        assert m0["index"] != m1["index"]


class TestDelegatePropagation:
    """parent_tool_call_id correlated via FIFO deque of pending delegate tool calls."""

    def setup_method(self):
        self.t = SessionEventTranslator()

    def test_session_fork_gets_parent_tool_call_id(self):
        self.t.translate(
            "tool:pre",
            {
                "tool_call_id": "tc-delegate-001",
                "tool_name": "delegate",
                "tool_input": {"agent": "explorer"},
            },
        )
        msg = self.t.translate(
            "delegate:agent_spawned",
            {
                "parent_id": "sess-parent",
                "child_id": "sess-child",
                "agent": "explorer",
            },
        )
        assert msg["type"] == "session_fork"
        assert msg["parent_tool_call_id"] == "tc-delegate-001"
        assert msg["parent_id"] == "sess-parent"
        assert msg["child_id"] == "sess-child"
        assert msg["agent"] == "explorer"

    def test_session_fork_no_pending_delegate(self):
        msg = self.t.translate(
            "delegate:agent_spawned",
            {
                "parent_id": "p",
                "child_id": "c",
                "agent": "x",
            },
        )
        assert msg["parent_tool_call_id"] is None

    def test_task_tool_also_tracked(self):
        self.t.translate(
            "tool:pre",
            {
                "tool_call_id": "tc-task-001",
                "tool_name": "task",
                "tool_input": {},
            },
        )
        msg = self.t.translate(
            "delegate:agent_spawned",
            {
                "parent_id": "p",
                "child_id": "c",
                "agent": "x",
            },
        )
        assert msg["parent_tool_call_id"] == "tc-task-001"

    def test_approval_request(self):
        msg = self.t.translate(
            "approval_request",
            {
                "request_id": "req-001",
                "prompt": "Allow tool?",
                "options": ["allow", "deny"],
                "timeout": 30.0,
                "default": "deny",
            },
        )
        assert msg == {
            "type": "approval_request",
            "id": "req-001",
            "prompt": "Allow tool?",
            "options": ["allow", "deny"],
            "timeout": 30.0,
            "default": "deny",
        }

    def test_server_index_is_public_static_method(self):
        """server_index (no underscore) is a public static method on the translator."""
        assert hasattr(SessionEventTranslator, "server_index")
        assert callable(SessionEventTranslator.server_index)
        # Verify it works as a static call (no instance needed)
        result = SessionEventTranslator.server_index({"block_index": 5})
        assert result == 5

    def test_block_text_is_public_static_method(self):
        """block_text (no underscore) is a public static method on the translator."""
        assert hasattr(SessionEventTranslator, "block_text")
        assert callable(SessionEventTranslator.block_text)
        result = SessionEventTranslator.block_text({"block": {"text": "hello"}})
        assert result == "hello"

    def test_fifo_order_for_parallel_delegates(self):
        self.t.translate(
            "tool:pre",
            {"tool_call_id": "tc-first", "tool_name": "delegate", "tool_input": {}},
        )
        self.t.translate(
            "tool:pre",
            {"tool_call_id": "tc-second", "tool_name": "delegate", "tool_input": {}},
        )
        msg1 = self.t.translate(
            "delegate:agent_spawned", {"parent_id": "p", "child_id": "c1", "agent": "x"}
        )
        msg2 = self.t.translate(
            "delegate:agent_spawned", {"parent_id": "p", "child_id": "c2", "agent": "y"}
        )
        assert msg1["parent_tool_call_id"] == "tc-first"
        assert msg2["parent_tool_call_id"] == "tc-second"
