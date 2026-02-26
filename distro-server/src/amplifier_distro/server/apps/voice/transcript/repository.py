"""Voice conversation repository for disk-backed persistence.

Disk layout:
  ~/.amplifier/voice-sessions/index.json           (fast listing)
  ~/.amplifier/voice-sessions/{session_id}/conversation.json  (atomic write)
  ~/.amplifier/voice-sessions/{session_id}/transcript.jsonl   (append-only)
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from amplifier_distro.server.apps.voice.transcript.models import (
    TranscriptEntry,
    VoiceConversation,
)


class VoiceConversationRepository:
    """Disk-backed repository for voice conversations and transcripts.

    Design rules:
    - index.json is ONLY rewritten on create_conversation(), end_conversation(),
      update_status(). NEVER touched by add_entry().
    - conversation.json is written atomically via .tmp -> rename
    - transcript.jsonl is append-only, never rewritten
    """

    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = base_dir or Path.home() / ".amplifier" / "voice-sessions"
        self._index_path = self.base_dir / "index.json"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write_atomic(self, path: Path, data: dict[str, Any]) -> None:
        """Write JSON atomically via .tmp -> rename."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        tmp.rename(path)

    def _read_index(self) -> list[dict[str, Any]]:
        """Read current index; return empty list if not present."""
        if not self._index_path.exists():
            return []
        return json.loads(self._index_path.read_text())  # type: ignore[no-any-return]

    def _write_index(self, entries: list[dict[str, Any]]) -> None:
        """Atomically overwrite index.json."""
        self._write_atomic(self._index_path, entries)  # type: ignore[arg-type]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_conversation(self, conv: VoiceConversation) -> None:
        """Create session directory, touch transcript.jsonl, write conversation.json
        and update index.json."""
        session_dir = self.base_dir / conv.id
        session_dir.mkdir(parents=True, exist_ok=True)

        # Touch transcript.jsonl (append-only file starts empty)
        (session_dir / "transcript.jsonl").touch()

        # Atomic write conversation.json
        self._write_atomic(session_dir / "conversation.json", conv.to_dict())

        # Update index
        index = self._read_index()
        index.append(
            {
                "id": conv.id,
                "title": conv.title,
                "status": conv.status,
                "created_at": conv.created_at.isoformat(),
            }
        )
        self._write_index(index)

    def get_conversation(self, session_id: str) -> VoiceConversation | None:
        """Return VoiceConversation for session_id, or None if not found."""
        conv_path = self.base_dir / session_id / "conversation.json"
        if not conv_path.exists():
            return None
        return VoiceConversation.from_dict(json.loads(conv_path.read_text()))

    def update_conversation(self, conv: VoiceConversation) -> None:
        """Atomic write of conversation.json only. Does NOT touch index.json."""
        session_dir = self.base_dir / conv.id
        self._write_atomic(session_dir / "conversation.json", conv.to_dict())

    def update_status(self, session_id: str, status: str) -> None:
        """Update status in both conversation.json and index.json."""
        conv = self.get_conversation(session_id)
        if conv is None:
            return
        conv.status = status
        conv.updated_at = datetime.now(UTC)
        self._write_atomic(
            self.base_dir / session_id / "conversation.json", conv.to_dict()
        )
        index = self._read_index()
        for item in index:
            if item["id"] == session_id:
                item["status"] = status
                break
        self._write_index(index)

    def end_conversation(self, session_id: str, reason: str) -> None:
        """Set status='ended', end_reason, ended_at, duration_seconds.
        Updates both conversation.json and index.json."""
        conv = self.get_conversation(session_id)
        if conv is None:
            return
        now = datetime.now(UTC)
        conv.status = "ended"
        conv.end_reason = reason
        conv.ended_at = now
        conv.updated_at = now
        conv.duration_seconds = (now - conv.created_at).total_seconds()
        self._write_atomic(
            self.base_dir / session_id / "conversation.json", conv.to_dict()
        )
        index = self._read_index()
        for item in index:
            if item["id"] == session_id:
                item["status"] = "ended"
                item["end_reason"] = reason
                break
        self._write_index(index)

    def add_entry(self, session_id: str, entry: TranscriptEntry) -> None:
        """Append one entry to transcript.jsonl. Does NOT touch index.json."""
        jsonl_path = self.base_dir / session_id / "transcript.jsonl"
        with jsonl_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")

    def add_entries(self, session_id: str, entries: list[TranscriptEntry]) -> None:
        """Batch-append entries to transcript.jsonl."""
        jsonl_path = self.base_dir / session_id / "transcript.jsonl"
        with jsonl_path.open("a", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")

    def get_resumption_context(self, session_id: str) -> list[dict[str, Any]]:
        """Read transcript.jsonl and return items in OpenAI Realtime API format.

        Mapping:
          user        -> message/user    content=[{type: input_text}]
          assistant   -> message/asst    content=[{type: output_text}]
          tool_call   -> function_call   {name, call_id, arguments}
          tool_result -> function_call_output  {call_id, output}
        """
        jsonl_path = self.base_dir / session_id / "transcript.jsonl"
        if not jsonl_path.exists():
            return []

        items: list[dict[str, Any]] = []
        for line in jsonl_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            entry_data: dict[str, Any] = json.loads(line)
            role = entry_data.get("role", "")
            content = entry_data.get("content", "")

            if role == "user":
                items.append(
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": content}],
                    }
                )
            elif role == "assistant":
                items.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": content}],
                    }
                )
            elif role == "tool_call":
                items.append(
                    {
                        "type": "function_call",
                        "name": entry_data.get("tool_name"),
                        "call_id": entry_data.get("call_id"),
                        "arguments": content,
                    }
                )
            elif role == "tool_result":
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": entry_data.get("call_id"),
                        "output": content,
                    }
                )

        return items

    def list_conversations(self) -> list[dict[str, Any]]:
        """Return all conversations from index.json (fast listing)."""
        return self._read_index()
