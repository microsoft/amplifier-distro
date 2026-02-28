"""Tests for voice transcript models: VoiceConversation, TranscriptEntry,
DisconnectEvent, and VoiceConversationRepository."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from amplifier_distro.server.apps.voice.transcript.models import (
    DisconnectEvent,
    TranscriptEntry,
    VoiceConversation,
    new_entry_id,
)
from amplifier_distro.server.apps.voice.transcript.repository import (
    VoiceConversationRepository,
)


class TestVoiceConversation:
    """Tests for VoiceConversation dataclass."""

    def _make_conversation(self, **kwargs) -> VoiceConversation:
        defaults = {
            "id": "session-abc-123",
            "title": "Test Conversation",
            "status": "active",
            "created_at": datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC),
            "updated_at": datetime(2024, 1, 15, 10, 5, 0, tzinfo=UTC),
        }
        defaults.update(kwargs)
        return VoiceConversation(**defaults)

    def test_round_trip_to_dict_from_dict(self) -> None:
        """VoiceConversation survives to_dict/from_dict round-trip."""
        conv = self._make_conversation(
            ended_at=datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC),
            end_reason="user_ended",
            duration_seconds=1800.5,
            first_message="Hello",
            last_message="Goodbye",
            tool_call_count=3,
            reconnect_count=1,
        )
        d = conv.to_dict()
        restored = VoiceConversation.from_dict(d)

        assert restored.id == conv.id
        assert restored.title == conv.title
        assert restored.status == conv.status
        assert restored.ended_at == conv.ended_at
        assert restored.end_reason == conv.end_reason
        assert restored.duration_seconds == conv.duration_seconds
        assert restored.first_message == conv.first_message
        assert restored.last_message == conv.last_message
        assert restored.tool_call_count == conv.tool_call_count
        assert restored.reconnect_count == conv.reconnect_count

    def test_omits_none_values_in_to_dict(self) -> None:
        """to_dict() omits ended_at, end_reason, duration_seconds when None."""
        conv = self._make_conversation()  # no ended_at, end_reason, duration_seconds

        d = conv.to_dict()

        assert "ended_at" not in d
        assert "end_reason" not in d
        assert "duration_seconds" not in d
        # Non-None fields should still be present
        assert "id" in d
        assert "title" in d
        assert "status" in d

    def test_from_dict_ignores_unknown_keys(self) -> None:
        """from_dict() silently ignores unknown keys."""
        data = {
            "id": "session-xyz",
            "title": "Test",
            "status": "active",
            "created_at": "2024-01-15T10:00:00+00:00",
            "updated_at": "2024-01-15T10:05:00+00:00",
            "unknown_field": "should be ignored",
            "another_unknown": 42,
        }
        # Should not raise
        conv = VoiceConversation.from_dict(data)
        assert conv.id == "session-xyz"
        assert conv.title == "Test"

    def test_end_reason_valid_values(self) -> None:
        """end_reason accepts all valid values."""
        valid_reasons = [
            "session_limit",
            "network_error",
            "user_ended",
            "idle_timeout",
            "error",
        ]
        for reason in valid_reasons:
            conv = self._make_conversation(end_reason=reason)
            assert conv.end_reason == reason


class TestTranscriptEntry:
    """Tests for TranscriptEntry dataclass."""

    def _make_entry(self, **kwargs) -> TranscriptEntry:
        defaults = {
            "id": "entry-001",
            "conversation_id": "session-abc-123",
            "role": "user",
            "content": "Hello, how are you?",
            "created_at": datetime(2024, 1, 15, 10, 1, 0, tzinfo=UTC),
        }
        defaults.update(kwargs)
        return TranscriptEntry(**defaults)

    def test_round_trip_to_dict_from_dict(self) -> None:
        """TranscriptEntry survives to_dict/from_dict round-trip."""
        entry = self._make_entry(
            role="assistant",
            content="I am doing well, thank you!",
            audio_duration_ms=3500,
            item_id="item-abc",
        )
        d = entry.to_dict()
        restored = TranscriptEntry.from_dict(d)

        assert restored.id == entry.id
        assert restored.conversation_id == entry.conversation_id
        assert restored.role == entry.role
        assert restored.content == entry.content
        assert restored.audio_duration_ms == entry.audio_duration_ms
        assert restored.item_id == entry.item_id
        assert restored.created_at == entry.created_at

    def test_from_dict_ignores_unknown_keys(self) -> None:
        """from_dict() silently ignores unknown keys."""
        data = {
            "id": "entry-002",
            "conversation_id": "session-xyz",
            "role": "user",
            "content": "Hi there",
            "created_at": "2024-01-15T10:01:00+00:00",
            "totally_unknown": "ignore me",
            "future_field": {"nested": True},
        }
        entry = TranscriptEntry.from_dict(data)
        assert entry.id == "entry-002"
        assert entry.content == "Hi there"

    def test_tool_call_entry_has_call_id_and_tool_name(self) -> None:
        """tool_call role entry stores call_id and tool_name."""
        entry = self._make_entry(
            role="tool_call",
            content='{"action": "search"}',
            call_id="call-xyz-789",
            tool_name="web_search",
        )
        d = entry.to_dict()

        assert d["role"] == "tool_call"
        assert d["call_id"] == "call-xyz-789"
        assert d["tool_name"] == "web_search"

        # Verify round-trip preserves these
        restored = TranscriptEntry.from_dict(d)
        assert restored.call_id == "call-xyz-789"
        assert restored.tool_name == "web_search"


class TestNewEntryId:
    """Tests for new_entry_id() helper function."""

    def test_returns_string(self) -> None:
        result = new_entry_id()
        assert isinstance(result, str)

    def test_returns_unique_values(self) -> None:
        ids = {new_entry_id() for _ in range(10)}
        assert len(ids) == 10


class TestDisconnectEvent:
    """Tests for DisconnectEvent dataclass."""

    def test_round_trip_to_dict_from_dict(self) -> None:
        event = DisconnectEvent(
            timestamp="2024-01-15T10:10:00Z",
            reason="network_error",
            reconnected=True,
        )
        d = event.to_dict()
        restored = DisconnectEvent.from_dict(d)

        assert restored.timestamp == event.timestamp
        assert restored.reason == event.reason
        assert restored.reconnected == event.reconnected

    def test_default_reconnected_is_false(self) -> None:
        event = DisconnectEvent(timestamp="2024-01-15T10:10:00Z", reason="idle_timeout")
        assert event.reconnected is False


class TestVoiceConversationRepository:
    """Tests for VoiceConversationRepository disk-backed persistence."""

    @pytest.fixture()
    def repo(self, tmp_path: Path) -> VoiceConversationRepository:
        return VoiceConversationRepository(base_dir=tmp_path)

    def _make_conversation(self, session_id: str = "sess-001") -> VoiceConversation:
        now = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)
        return VoiceConversation(
            id=session_id,
            title="Test Session",
            status="active",
            created_at=now,
            updated_at=now,
        )

    def _make_entry(
        self,
        session_id: str = "sess-001",
        role: str = "user",
        content: str = "Hello",
        tool_name: str | None = None,
        call_id: str | None = None,
        item_id: str | None = None,
        audio_duration_ms: int | None = None,
    ) -> TranscriptEntry:
        return TranscriptEntry(
            id=new_entry_id(),
            conversation_id=session_id,
            role=role,
            content=content,
            created_at=datetime(2024, 1, 15, 10, 1, 0, tzinfo=UTC),
            tool_name=tool_name,
            call_id=call_id,
            item_id=item_id,
            audio_duration_ms=audio_duration_ms,
        )

    def test_create_conversation_writes_files(
        self, repo: VoiceConversationRepository, tmp_path: Path
    ) -> None:
        """create_conversation() writes conversation.json and updates index.json."""
        conv = self._make_conversation()
        repo.create_conversation(conv)

        conv_json = tmp_path / "sess-001" / "conversation.json"
        index_json = tmp_path / "index.json"

        assert conv_json.exists(), "conversation.json must be created"
        assert index_json.exists(), "index.json must be created"

        # conversation.json must contain valid data
        data = json.loads(conv_json.read_text())
        assert data["id"] == "sess-001"
        assert data["status"] == "active"

        # index.json must contain the conversation
        index = json.loads(index_json.read_text())
        assert len(index) == 1
        assert index[0]["id"] == "sess-001"

    def test_get_conversation_returns_correct_data(
        self, repo: VoiceConversationRepository
    ) -> None:
        """get_conversation() returns the stored VoiceConversation."""
        conv = self._make_conversation()
        repo.create_conversation(conv)

        result = repo.get_conversation("sess-001")

        assert result is not None
        assert result.id == "sess-001"
        assert result.title == "Test Session"
        assert result.status == "active"

    def test_get_conversation_returns_none_if_not_found(
        self, repo: VoiceConversationRepository
    ) -> None:
        """get_conversation() returns None for unknown session_id."""
        result = repo.get_conversation("nonexistent")
        assert result is None

    def test_add_entry_does_not_touch_index_json(
        self, repo: VoiceConversationRepository, tmp_path: Path
    ) -> None:
        """add_entry() must NOT modify index.json (mtime unchanged)."""
        conv = self._make_conversation()
        repo.create_conversation(conv)

        index_json = tmp_path / "index.json"
        mtime_before = index_json.stat().st_mtime

        # Small sleep to ensure mtime would differ if file were written
        time.sleep(0.05)

        entry = self._make_entry()
        repo.add_entry("sess-001", entry)

        mtime_after = index_json.stat().st_mtime
        assert mtime_before == mtime_after, "add_entry() must NOT touch index.json"

    def test_add_entry_appends_to_jsonl(
        self, repo: VoiceConversationRepository, tmp_path: Path
    ) -> None:
        """add_entry() appends lines; 3 entries = 3 non-empty lines in jsonl."""
        conv = self._make_conversation()
        repo.create_conversation(conv)

        for i in range(3):
            entry = self._make_entry(content=f"message {i}")
            repo.add_entry("sess-001", entry)

        jsonl_path = tmp_path / "sess-001" / "transcript.jsonl"
        lines = [ln for ln in jsonl_path.read_text().splitlines() if ln.strip()]
        assert len(lines) == 3, f"Expected 3 lines, got {len(lines)}"

        # Each line must be valid JSON
        for line in lines:
            parsed = json.loads(line)
            assert "role" in parsed

    def test_end_conversation_updates_index(
        self, repo: VoiceConversationRepository, tmp_path: Path
    ) -> None:
        """end_conversation() sets status='ended' in index.json and end_reason."""
        conv = self._make_conversation()
        repo.create_conversation(conv)

        repo.end_conversation("sess-001", reason="user_ended")

        index = json.loads((tmp_path / "index.json").read_text())
        assert len(index) == 1
        assert index[0]["status"] == "ended"
        assert index[0]["end_reason"] == "user_ended"

        # conversation.json must also reflect ended status
        result = repo.get_conversation("sess-001")
        assert result is not None
        assert result.status == "ended"
        assert result.end_reason == "user_ended"
        assert result.ended_at is not None
        assert result.duration_seconds is not None

    def test_get_resumption_context_includes_tool_calls(
        self, repo: VoiceConversationRepository
    ) -> None:
        """get_resumption_context() maps tool_call->function_call and
        tool_result->function_call_output."""
        conv = self._make_conversation()
        repo.create_conversation(conv)

        entries = [
            self._make_entry(role="user", content="What is the weather?"),
            self._make_entry(
                role="tool_call",
                content='{"location": "NYC"}',
                tool_name="get_weather",
                call_id="call-abc-123",
            ),
            self._make_entry(
                role="tool_result",
                content='{"temp": "72F"}',
                call_id="call-abc-123",
            ),
            self._make_entry(role="assistant", content="It is 72F in NYC."),
        ]
        repo.add_entries("sess-001", entries)

        context = repo.get_resumption_context("sess-001")

        assert len(context) == 4

        # user message
        assert context[0]["type"] == "message"
        assert context[0]["role"] == "user"
        assert context[0]["content"][0]["type"] == "input_text"
        assert context[0]["content"][0]["text"] == "What is the weather?"

        # tool_call -> function_call
        assert context[1]["type"] == "function_call"
        assert context[1]["name"] == "get_weather"
        assert context[1]["call_id"] == "call-abc-123"

        # tool_result -> function_call_output
        assert context[2]["type"] == "function_call_output"
        assert context[2]["call_id"] == "call-abc-123"

        # assistant message
        assert context[3]["type"] == "message"
        assert context[3]["role"] == "assistant"
        assert context[3]["content"][0]["type"] == "output_text"

    def test_conversation_json_written_atomically(
        self, repo: VoiceConversationRepository, tmp_path: Path
    ) -> None:
        """create_conversation() leaves no .tmp files behind."""
        conv = self._make_conversation()
        repo.create_conversation(conv)

        tmp_files = list(tmp_path.rglob("*.tmp"))
        assert len(tmp_files) == 0, f"Leftover .tmp files: {tmp_files}"


class TestWriteToAmplifierTranscript:
    """Tests for VoiceConversationRepository.write_to_amplifier_transcript().

    This method mirrors voice conversation turns into the Amplifier session
    transcript at ~/.amplifier/projects/{project_id}/sessions/{session_id}/
    transcript.jsonl
    so that the chat app can discover and display voice sessions.
    """

    @pytest.fixture()
    def repo(self, tmp_path: Path) -> VoiceConversationRepository:
        return VoiceConversationRepository(base_dir=tmp_path / "voice-sessions")

    @pytest.fixture()
    def amplifier_home(self, tmp_path: Path) -> Path:
        home = tmp_path / "amplifier-home"
        home.mkdir()
        return home

    def _make_entry(
        self,
        role: str = "user",
        content: str = "Hello",
        session_id: str = "sess-001",
    ) -> TranscriptEntry:
        return TranscriptEntry(
            id=new_entry_id(),
            conversation_id=session_id,
            role=role,
            content=content,
            created_at=datetime(2024, 1, 15, 10, 1, 0, tzinfo=UTC),
        )

    def _amplifier_transcript(
        self, amplifier_home: Path, project_id: str, session_id: str
    ) -> Path:
        return (
            amplifier_home
            / "projects"
            / project_id
            / "sessions"
            / session_id
            / "transcript.jsonl"
        )

    def test_write_to_amplifier_transcript_creates_file(
        self, repo: VoiceConversationRepository, amplifier_home: Path
    ) -> None:
        """write_to_amplifier_transcript() creates transcript.jsonl with user/assistant lines."""  # noqa: E501
        entries = [
            self._make_entry(role="user", content="Hello"),
            self._make_entry(role="assistant", content="Hi there!"),
        ]

        repo.write_to_amplifier_transcript(
            "sess-001", "proj-123", entries, amplifier_home=amplifier_home
        )

        transcript_path = self._amplifier_transcript(
            amplifier_home, "proj-123", "sess-001"
        )
        assert transcript_path.exists(), "transcript.jsonl must be created"

        lines = [ln for ln in transcript_path.read_text().splitlines() if ln.strip()]
        assert len(lines) == 2

        first = json.loads(lines[0])
        assert first["role"] == "user"
        assert first["content"] == [{"type": "text", "text": "Hello"}]

        second = json.loads(lines[1])
        assert second["role"] == "assistant"
        assert second["content"] == [{"type": "text", "text": "Hi there!"}]

    def test_write_to_amplifier_transcript_skips_tool_entries(
        self, repo: VoiceConversationRepository, amplifier_home: Path
    ) -> None:
        """tool_call and tool_result entries must NOT appear in the Amplifier transcript."""  # noqa: E501
        entries = [
            self._make_entry(role="user", content="Do something"),
            self._make_entry(role="tool_call", content='{"action": "search"}'),
            self._make_entry(role="tool_result", content='{"result": "done"}'),
            self._make_entry(role="assistant", content="Done!"),
        ]

        repo.write_to_amplifier_transcript(
            "sess-001", "proj-123", entries, amplifier_home=amplifier_home
        )

        transcript_path = self._amplifier_transcript(
            amplifier_home, "proj-123", "sess-001"
        )
        lines = [ln for ln in transcript_path.read_text().splitlines() if ln.strip()]
        assert len(lines) == 2, (
            f"Expected 2 lines (user + assistant only), got {len(lines)}"
        )

        roles = [json.loads(ln)["role"] for ln in lines]
        assert roles == ["user", "assistant"]

    def test_write_to_amplifier_transcript_empty_entries_creates_stub(
        self, repo: VoiceConversationRepository, amplifier_home: Path
    ) -> None:
        """Calling with [] must create directory and empty transcript.jsonl for discoverability."""  # noqa: E501
        repo.write_to_amplifier_transcript(
            "sess-001", "proj-123", [], amplifier_home=amplifier_home
        )

        transcript_path = self._amplifier_transcript(
            amplifier_home, "proj-123", "sess-001"
        )
        assert transcript_path.exists(), (
            "transcript.jsonl must be created even with no entries"
        )
        assert transcript_path.read_text() == "", (
            "stub file must be empty when no entries"
        )

    def test_write_to_amplifier_transcript_appends(
        self, repo: VoiceConversationRepository, amplifier_home: Path
    ) -> None:
        """Calling write_to_amplifier_transcript twice appends rather than overwrites."""  # noqa: E501
        first_call = [self._make_entry(role="user", content="First message")]
        second_call = [self._make_entry(role="assistant", content="Second message")]

        repo.write_to_amplifier_transcript(
            "sess-001", "proj-123", first_call, amplifier_home=amplifier_home
        )
        repo.write_to_amplifier_transcript(
            "sess-001", "proj-123", second_call, amplifier_home=amplifier_home
        )

        transcript_path = self._amplifier_transcript(
            amplifier_home, "proj-123", "sess-001"
        )
        lines = [ln for ln in transcript_path.read_text().splitlines() if ln.strip()]
        assert len(lines) == 2, "Second call must append, not overwrite"
        assert json.loads(lines[0])["role"] == "user"
        assert json.loads(lines[1])["role"] == "assistant"
