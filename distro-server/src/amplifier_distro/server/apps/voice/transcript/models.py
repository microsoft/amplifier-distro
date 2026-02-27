"""Voice transcript data models for VoiceConversation and TranscriptEntry."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal


def new_entry_id() -> str:
    """Return a new unique entry ID."""
    return str(uuid.uuid4())


def _parse_datetime(value: Any) -> datetime:
    """Parse a datetime from either a datetime object or ISO format string."""
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _omit_none(d: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of d with all None-valued keys removed."""
    return {k: v for k, v in d.items() if v is not None}


@dataclass
class DisconnectEvent:
    """Records a single disconnect event during a voice conversation."""

    timestamp: str
    reason: str
    reconnected: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict."""
        return {
            "timestamp": self.timestamp,
            "reason": self.reason,
            "reconnected": self.reconnected,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DisconnectEvent:
        """Deserialize from dict, ignoring unknown keys."""
        return cls(
            timestamp=data["timestamp"],
            reason=data["reason"],
            reconnected=data.get("reconnected", False),
        )


@dataclass
class VoiceConversation:
    """Represents a voice conversation session.

    The id IS the Amplifier session ID — no separate voice session UUID.
    """

    id: str
    title: str
    status: Literal["active", "disconnected", "ended"]
    created_at: datetime
    updated_at: datetime
    ended_at: datetime | None = None
    end_reason: (
        Literal["session_limit", "network_error", "user_ended", "idle_timeout", "error"]
        | None
    ) = None
    duration_seconds: float | None = None
    first_message: str | None = None
    last_message: str | None = None
    tool_call_count: int = 0
    reconnect_count: int = 0
    disconnect_history: list[DisconnectEvent] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict, omitting None values."""
        result: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "ended_at": self.ended_at.isoformat()
            if self.ended_at is not None
            else None,
            "end_reason": self.end_reason,
            "duration_seconds": self.duration_seconds,
            "first_message": self.first_message,
            "last_message": self.last_message,
            "tool_call_count": self.tool_call_count,
            "reconnect_count": self.reconnect_count,
            "disconnect_history": [e.to_dict() for e in self.disconnect_history],
        }
        return _omit_none(result)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VoiceConversation:
        """Deserialize from dict, ignoring unknown keys."""
        disconnect_history_data = data.get("disconnect_history", [])
        return cls(
            id=data["id"],
            title=data["title"],
            status=data["status"],
            created_at=_parse_datetime(data["created_at"]),
            updated_at=_parse_datetime(data["updated_at"]),
            ended_at=_parse_datetime(data["ended_at"])
            if data.get("ended_at") is not None
            else None,
            end_reason=data.get("end_reason"),
            duration_seconds=data.get("duration_seconds"),
            first_message=data.get("first_message"),
            last_message=data.get("last_message"),
            tool_call_count=data.get("tool_call_count", 0),
            reconnect_count=data.get("reconnect_count", 0),
            disconnect_history=[
                DisconnectEvent.from_dict(e) for e in disconnect_history_data
            ],
        )


@dataclass
class TranscriptEntry:
    """A single entry in a voice conversation transcript."""

    id: str
    conversation_id: str
    role: str  # user | assistant | tool_call | tool_result
    content: str
    created_at: datetime
    audio_duration_ms: int | None = None
    item_id: str | None = None
    tool_name: str | None = None
    call_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict, omitting None values."""
        result: dict[str, Any] = {
            "id": self.id,
            "conversation_id": self.conversation_id,
            "role": self.role,
            "content": self.content,
            "created_at": self.created_at.isoformat(),
            "audio_duration_ms": self.audio_duration_ms,
            "item_id": self.item_id,
            "tool_name": self.tool_name,
            "call_id": self.call_id,
        }
        return _omit_none(result)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TranscriptEntry:
        """Deserialize from dict, ignoring unknown keys."""
        return cls(
            id=data["id"],
            conversation_id=data["conversation_id"],
            role=data["role"],
            content=data["content"],
            created_at=_parse_datetime(data["created_at"]),
            audio_duration_ms=data.get("audio_duration_ms"),
            item_id=data.get("item_id"),
            tool_name=data.get("tool_name"),
            call_id=data.get("call_id"),
        )
