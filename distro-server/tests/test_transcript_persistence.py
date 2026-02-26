# tests/test_transcript_persistence.py
"""Tests for transcript persistence during server sessions.

Covers:
- write_transcript: JSONL writing, role filtering, sanitization
- TranscriptSaveHook: debounce, best-effort, event handling
- register_transcript_hooks: registration on both events
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


class TestWriteTranscript:
    """Verify write_transcript writes valid JSONL and filters roles."""

    def test_writes_valid_jsonl(self, tmp_path: Path) -> None:
        """Messages are written as one JSON object per line."""
        from amplifier_distro.transcript_persistence import write_transcript

        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]
        write_transcript(tmp_path, messages)

        transcript = tmp_path / "transcript.jsonl"
        assert transcript.exists()
        lines = [
            json.loads(line) for line in transcript.read_text().strip().split("\n")
        ]
        assert len(lines) == 2
        assert lines[0] == {"role": "user", "content": "hello"}
        assert lines[1] == {"role": "assistant", "content": "hi there"}

    def test_filters_system_and_developer_roles(self, tmp_path: Path) -> None:
        """System and developer messages are excluded from transcript."""
        from amplifier_distro.transcript_persistence import write_transcript

        messages = [
            {"role": "system", "content": "you are helpful"},
            {"role": "developer", "content": "context injection"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        write_transcript(tmp_path, messages)

        transcript = tmp_path / "transcript.jsonl"
        lines = [
            json.loads(line) for line in transcript.read_text().strip().split("\n")
        ]
        assert len(lines) == 2
        assert lines[0]["role"] == "user"
        assert lines[1]["role"] == "assistant"

    def test_keeps_tool_role(self, tmp_path: Path) -> None:
        """Tool messages are preserved in transcript."""
        from amplifier_distro.transcript_persistence import write_transcript

        messages = [
            {"role": "user", "content": "list files"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_1", "function": {"name": "bash", "arguments": "{}"}}
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "file1.py"},
            {"role": "assistant", "content": "I found file1.py"},
        ]
        write_transcript(tmp_path, messages)

        transcript = tmp_path / "transcript.jsonl"
        lines = [
            json.loads(line) for line in transcript.read_text().strip().split("\n")
        ]
        assert len(lines) == 4
        assert lines[2]["role"] == "tool"

    def test_empty_messages_writes_empty_file(self, tmp_path: Path) -> None:
        """Empty message list produces an empty file (not no file)."""
        from amplifier_distro.transcript_persistence import write_transcript

        write_transcript(tmp_path, [])

        transcript = tmp_path / "transcript.jsonl"
        assert transcript.exists()
        assert transcript.read_text() == ""

    def test_creates_session_dir_if_missing(self, tmp_path: Path) -> None:
        """Parent directories are created if they don't exist."""
        from amplifier_distro.transcript_persistence import write_transcript

        deep_dir = tmp_path / "projects" / "test" / "sessions" / "abc123"
        write_transcript(deep_dir, [{"role": "user", "content": "hi"}])

        assert (deep_dir / "transcript.jsonl").exists()

    def test_preserves_content_null(self, tmp_path: Path) -> None:
        """Assistant tool-call messages with content:null preserve the field.

        This is critical: providers reject tool-call messages missing content.
        """
        from amplifier_distro.transcript_persistence import write_transcript

        messages = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "c1", "function": {"name": "f", "arguments": "{}"}}
                ],
            },
        ]
        write_transcript(tmp_path, messages)

        transcript = tmp_path / "transcript.jsonl"
        parsed = json.loads(transcript.read_text().strip())
        assert "content" in parsed, "content:null must be preserved, not dropped"
        assert parsed["content"] is None

    def test_handles_unicode(self, tmp_path: Path) -> None:
        """Unicode content survives the write round-trip."""
        from amplifier_distro.transcript_persistence import write_transcript

        messages = [
            {
                "role": "user",
                "content": "Fix the \u65e5\u672c\u8a9e handling \U0001f527",
            }
        ]
        write_transcript(tmp_path, messages)

        transcript = tmp_path / "transcript.jsonl"
        parsed = json.loads(transcript.read_text().strip())
        assert "\u65e5\u672c\u8a9e" in parsed["content"]

    def test_readable_by_resume_session_reader(self, tmp_path: Path) -> None:
        """Output is compatible with bridge.py resume_session() transcript reader.

        This is the round-trip test: write_transcript output must be parseable
        by the same line-by-line JSON reader used in resume_session() step 8.
        """
        from amplifier_distro.transcript_persistence import write_transcript

        messages = [
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "c1", "function": {"name": "bash", "arguments": "{}"}}
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
            {"role": "assistant", "content": "done"},
        ]
        write_transcript(tmp_path, messages)

        # Read back using the same logic as bridge.py resume_session() step 8
        transcript = tmp_path / "transcript.jsonl"
        loaded: list[dict] = []
        with transcript.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                if isinstance(entry, dict) and entry.get("role"):
                    loaded.append(entry)

        assert len(loaded) == 4
        assert loaded[0]["role"] == "user"
        assert loaded[1]["tool_calls"][0]["id"] == "c1"
        assert loaded[2]["role"] == "tool"
        assert loaded[3]["content"] == "done"


# --- Helper for hook tests ---------------------------------------------------


def _make_mock_session(messages: list[dict] | None = None) -> MagicMock:
    """Create a mock session with coordinator.get('context').get_messages().

    Mirrors the CLI pattern: hooks access context via coordinator.get('context'),
    not coordinator.context directly.
    """
    session = MagicMock()
    context = MagicMock()
    context.get_messages = AsyncMock(return_value=messages or [])
    session.coordinator.get = MagicMock(return_value=context)
    return session


class TestTranscriptSaveHook:
    """Verify hook debounce, best-effort, and event handling."""

    def test_writes_on_new_messages(self, tmp_path: Path) -> None:
        """Hook writes transcript when message count increases."""
        from amplifier_distro.transcript_persistence import TranscriptSaveHook

        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        session = _make_mock_session(messages)
        hook = TranscriptSaveHook(session, tmp_path)

        result = asyncio.run(hook("tool:post", {}))

        transcript = tmp_path / "transcript.jsonl"
        assert transcript.exists()
        assert result.action == "continue"

    def test_debounce_skips_when_count_unchanged(self, tmp_path: Path) -> None:
        """Hook skips write when message count hasn't changed."""
        from amplifier_distro.transcript_persistence import TranscriptSaveHook

        messages = [{"role": "user", "content": "hello"}]
        session = _make_mock_session(messages)
        hook = TranscriptSaveHook(session, tmp_path)

        # First call: writes
        asyncio.run(hook("tool:post", {}))
        assert (tmp_path / "transcript.jsonl").exists()

        # Second call with same count: should not re-write
        with patch(
            "amplifier_distro.transcript_persistence.write_transcript"
        ) as mock_wt:
            asyncio.run(hook("tool:post", {}))
        mock_wt.assert_not_called()

    def test_debounce_writes_when_count_increases(self, tmp_path: Path) -> None:
        """Hook writes again when message count increases between calls."""
        from amplifier_distro.transcript_persistence import TranscriptSaveHook

        session = _make_mock_session([{"role": "user", "content": "hello"}])
        hook = TranscriptSaveHook(session, tmp_path)

        # First call
        asyncio.run(hook("tool:post", {}))
        assert hook._last_count == 1

        # Update messages (simulating new messages after tool call)
        new_messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        context = session.coordinator.get("context")
        context.get_messages = AsyncMock(return_value=new_messages)

        # Second call with increased count
        asyncio.run(hook("orchestrator:complete", {}))
        assert hook._last_count == 2

    def test_best_effort_exception_does_not_propagate(self, tmp_path: Path) -> None:
        """Hook catches exceptions and returns continue -- never fails the loop."""
        from amplifier_distro.transcript_persistence import TranscriptSaveHook

        session = _make_mock_session()
        # Make get_messages raise
        context = session.coordinator.get("context")
        context.get_messages = AsyncMock(side_effect=RuntimeError("boom"))
        hook = TranscriptSaveHook(session, tmp_path)

        result = asyncio.run(hook("tool:post", {}))

        assert result.action == "continue"

    def test_handles_missing_context_module(self, tmp_path: Path) -> None:
        """Hook gracefully handles coordinator.get('context') returning None."""
        from amplifier_distro.transcript_persistence import TranscriptSaveHook

        session = MagicMock()
        session.coordinator.get = MagicMock(return_value=None)
        hook = TranscriptSaveHook(session, tmp_path)

        result = asyncio.run(hook("tool:post", {}))

        assert result.action == "continue"
        assert not (tmp_path / "transcript.jsonl").exists()

    def test_works_with_orchestrator_complete_event(self, tmp_path: Path) -> None:
        """Hook fires correctly on orchestrator:complete (not just tool:post)."""
        from amplifier_distro.transcript_persistence import TranscriptSaveHook

        messages = [{"role": "user", "content": "hello"}]
        session = _make_mock_session(messages)
        hook = TranscriptSaveHook(session, tmp_path)

        result = asyncio.run(hook("orchestrator:complete", {}))

        assert (tmp_path / "transcript.jsonl").exists()
        assert result.action == "continue"


class TestRegisterTranscriptHooks:
    """Verify hook registration on both events."""

    def test_registers_on_tool_post_and_orchestrator_complete(self) -> None:
        """Both tool:post and orchestrator:complete hooks are registered."""
        from amplifier_distro.transcript_persistence import register_transcript_hooks

        session = MagicMock()
        session.coordinator.hooks.register = MagicMock()
        session_dir = Path("/tmp/fake-session")

        register_transcript_hooks(session, session_dir)

        calls = session.coordinator.hooks.register.call_args_list
        assert len(calls) == 2

        events_registered = set()
        for call in calls:
            events_registered.add(
                call.kwargs.get(
                    "event", call[1].get("event") if len(call) > 1 else None
                )
            )
        assert "tool:post" in events_registered
        assert "orchestrator:complete" in events_registered

    def test_silently_noops_if_hooks_unavailable(self) -> None:
        """No exception if session has no hooks API."""
        from amplifier_distro.transcript_persistence import register_transcript_hooks

        session = MagicMock(spec=[])  # no attributes at all
        session_dir = Path("/tmp/fake-session")

        # Must not raise
        register_transcript_hooks(session, session_dir)


class TestResumeTranscriptPreservation:
    """Verify transcript.jsonl preserves old messages after resume.

    This is the critical end-to-end test: session creates transcript,
    server restarts, resume loads old messages via set_messages(),
    new messages are added, and the hook writes old + new together.
    """

    def test_hook_preserves_old_messages_after_resume(self, tmp_path: Path) -> None:
        """After resume, transcript contains old + new messages, not just new.

        Simulates:
        1. First session writes transcript with 2 messages
        2. Resume loads those 2 messages into context (set_messages)
        3. New message is added (user sends "message 3")
        4. Hook fires → transcript should have 3+ messages (old + new)
        """
        from amplifier_distro.transcript_persistence import (
            TranscriptSaveHook,
            write_transcript,
        )

        # Step 1: First session writes transcript with 2 messages
        old_messages = [
            {"role": "user", "content": "message 1"},
            {"role": "assistant", "content": "response 1"},
        ]
        write_transcript(tmp_path, old_messages)

        # Verify transcript has 2 lines
        transcript = tmp_path / "transcript.jsonl"
        assert len(transcript.read_text().strip().split("\n")) == 2

        # Step 2+3: After resume, context has old + new messages
        # (set_messages loaded the old ones, orchestrator added new ones)
        resumed_messages = [
            {"role": "user", "content": "message 1"},
            {"role": "assistant", "content": "response 1"},
            {"role": "user", "content": "message 2"},
            {"role": "assistant", "content": "response 2"},
        ]
        session = _make_mock_session(resumed_messages)
        hook = TranscriptSaveHook(session, tmp_path)

        # Step 4: Hook fires after orchestrator:complete
        asyncio.run(hook("orchestrator:complete", {}))

        # Verify transcript has ALL 4 messages (old + new), not just 2
        lines = [
            json.loads(line) for line in transcript.read_text().strip().split("\n")
        ]
        assert len(lines) == 4, (
            f"Expected 4 messages (old + new) but got {len(lines)}. "
            "Transcript was overwritten instead of preserving old messages."
        )
        assert lines[0]["content"] == "message 1"
        assert lines[1]["content"] == "response 1"
        assert lines[2]["content"] == "message 2"
        assert lines[3]["content"] == "response 2"

    def test_hook_does_not_shrink_transcript_on_resume(self, tmp_path: Path) -> None:
        """Hook must never write fewer messages than already on disk.

        If context.get_messages() returns only post-resume messages
        (a bug in the resume flow), the hook should still write them —
        but this test documents the EXPECTED behavior: get_messages()
        should return old + new after set_messages() was called.
        """
        from amplifier_distro.transcript_persistence import (
            TranscriptSaveHook,
            write_transcript,
        )

        # Original transcript has 4 messages
        original_messages = [
            {"role": "user", "content": "msg 1"},
            {"role": "assistant", "content": "resp 1"},
            {"role": "user", "content": "msg 2"},
            {"role": "assistant", "content": "resp 2"},
        ]
        write_transcript(tmp_path, original_messages)

        # After resume, context has old 4 + new 2 = 6 messages
        all_messages = original_messages + [
            {"role": "user", "content": "msg 3"},
            {"role": "assistant", "content": "resp 3"},
        ]
        session = _make_mock_session(all_messages)
        hook = TranscriptSaveHook(session, tmp_path)

        asyncio.run(hook("orchestrator:complete", {}))

        lines = [
            json.loads(line)
            for line in tmp_path.joinpath("transcript.jsonl")
            .read_text()
            .strip()
            .split("\n")
        ]
        assert len(lines) == 6, (
            f"Expected 6 messages but got {len(lines)}. "
            "Transcript shrank instead of growing."
        )
        assert lines[4]["content"] == "msg 3"
        assert lines[5]["content"] == "resp 3"
