"""Data models for the Slack bridge.

Defines the core data structures used throughout the bridge:
- Slack-side models (messages, channels, users)
- Bridge mapping models (session-to-channel mappings)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class ChannelType(StrEnum):
    """Type of Slack channel in the bridge context."""

    HUB = "hub"  # The main #amplifier channel
    SESSION = "session"  # A breakout channel for a specific session


@dataclass
class SlackUser:
    """A Slack user."""

    id: str
    name: str
    display_name: str = ""


@dataclass
class SlackChannel:
    """A Slack channel."""

    id: str
    name: str
    channel_type: ChannelType = ChannelType.HUB
    topic: str = ""
    created_at: str = ""


@dataclass
class SlackMessage:
    """A message from Slack."""

    channel_id: str
    user_id: str
    text: str
    ts: str  # Slack timestamp (unique message ID)
    thread_ts: str | None = None  # Parent thread timestamp (None = top-level)
    user_name: str = ""

    @property
    def is_threaded(self) -> bool:
        """Whether this message is in a thread."""
        return self.thread_ts is not None

    @property
    def conversation_key(self) -> str:
        """Unique key for the conversation context (channel + thread)."""
        if self.thread_ts:
            return f"{self.channel_id}:{self.thread_ts}"
        return self.channel_id


@dataclass
class SessionMapping:
    """Maps a Slack conversation context to an Amplifier session.

    A mapping ties a Slack channel (or thread within a channel) to
    an Amplifier session. This is the core routing table for the bridge.
    """

    session_id: str
    channel_id: str
    thread_ts: str | None = None  # None = entire channel is the session
    project_id: str = ""
    description: str = ""
    created_by: str = ""  # Slack user ID who created this
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    last_active: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    is_active: bool = True
    working_dir: str = ""  # Where this session operates (e.g., "~/repo/foo")

    @property
    def conversation_key(self) -> str:
        """Unique key matching SlackMessage.conversation_key."""
        if self.thread_ts:
            return f"{self.channel_id}:{self.thread_ts}"
        return self.channel_id
