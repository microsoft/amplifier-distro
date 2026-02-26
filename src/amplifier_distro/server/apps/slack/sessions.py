"""Session management - maps Slack conversations to Amplifier sessions.

The SlackSessionManager is the routing core of the bridge. It maintains
a mapping table between Slack conversation contexts (channel_id + thread_ts)
and Amplifier session IDs, and delegates message handling to the backend.

Conversation model:
- Hub channel: Commands and session creation happen here
- Thread: Each new session starts as a thread in the hub channel
- Breakout channel: A thread can be promoted to its own channel

The conversation_key is "channel_id:thread_ts" for threads, or just
"channel_id" for top-level channel conversations.

Persistence:
- Session mappings are persisted to a JSON file so they survive restarts.
- The file path comes from conventions.py (SLACK_SESSIONS_FILENAME).
- Mappings are loaded on startup and saved on every change.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from amplifier_distro.conventions import (
    AMPLIFIER_HOME,
    SERVER_DIR,
    SLACK_SESSIONS_FILENAME,
)

from .backend import SessionBackend
from .client import SlackClient
from .config import SlackConfig
from .models import SessionMapping, SlackChannel, SlackMessage

logger = logging.getLogger(__name__)


def _default_persistence_path() -> Path:
    """Return the default path for session persistence file."""
    return Path(AMPLIFIER_HOME).expanduser() / SERVER_DIR / SLACK_SESSIONS_FILENAME


class SlackSessionManager:
    """Manages Slack-to-Amplifier session mappings.

    This is the core routing table. When a message comes in from Slack,
    the manager looks up which Amplifier session it belongs to and
    routes the message through the backend.

    Session mappings are optionally persisted to disk as JSON so they
    survive server restarts. Pass persistence_path=None to disable
    persistence (useful in tests).
    """

    def __init__(
        self,
        client: SlackClient,
        backend: SessionBackend,
        config: SlackConfig,
        persistence_path: Path | None = None,
    ) -> None:
        self._client = client
        self._backend = backend
        self._config = config
        self._persistence_path = persistence_path
        self._mappings: dict[str, SessionMapping] = {}
        # Track which channels are breakout channels
        self._breakout_channels: dict[str, str] = {}  # channel_id -> session_id
        # Load persisted sessions on startup
        self._load_sessions()

    def _load_sessions(self) -> None:
        """Load session mappings from the persistence file."""
        if self._persistence_path is None or not self._persistence_path.exists():
            return
        try:
            data = json.loads(self._persistence_path.read_text())
            for entry in data:
                mapping = SessionMapping(
                    session_id=entry["session_id"],
                    channel_id=entry["channel_id"],
                    thread_ts=entry.get("thread_ts"),
                    project_id=entry.get("project_id", ""),
                    description=entry.get("description", ""),
                    created_by=entry.get("created_by", ""),
                    created_at=entry.get("created_at", ""),
                    last_active=entry.get("last_active", ""),
                    is_active=entry.get("is_active", True),
                    working_dir=entry.get("working_dir", ""),
                )
                key = mapping.conversation_key
                self._mappings[key] = mapping
            logger.info(
                f"Loaded {len(data)} session mappings from {self._persistence_path}"
            )
        except (json.JSONDecodeError, KeyError, OSError):
            logger.warning("Failed to load session mappings", exc_info=True)

    def _save_sessions(self) -> None:
        """Save session mappings to the persistence file."""
        if self._persistence_path is None:
            return
        try:
            self._persistence_path.parent.mkdir(parents=True, exist_ok=True)
            data = [
                {
                    "session_id": m.session_id,
                    "channel_id": m.channel_id,
                    "thread_ts": m.thread_ts,
                    "project_id": m.project_id,
                    "description": m.description,
                    "created_by": m.created_by,
                    "created_at": m.created_at,
                    "last_active": m.last_active,
                    "is_active": m.is_active,
                    "working_dir": m.working_dir,
                }
                for m in self._mappings.values()
            ]
            from amplifier_distro.fileutil import atomic_write

            atomic_write(self._persistence_path, json.dumps(data, indent=2))
        except OSError:
            logger.warning("Failed to save session mappings", exc_info=True)

    @property
    def mappings(self) -> dict[str, SessionMapping]:
        """Current mappings (read-only view)."""
        return dict(self._mappings)

    def get_mapping(
        self, channel_id: str, thread_ts: str | None = None
    ) -> SessionMapping | None:
        """Find the session mapping for a Slack conversation context."""
        # Thread-specific lookup: exact match only, no bare-channel fallback.
        # When thread_ts is provided the caller is asking about a specific
        # thread; falling back to a bare-channel key would silently match
        # unrelated sessions and is the root cause of issue #54.
        if thread_ts:
            key = f"{channel_id}:{thread_ts}"
            return self._mappings.get(key)

        # Bare-channel lookup (breakout channels and top-level sessions).
        if channel_id in self._mappings:
            return self._mappings[channel_id]

        # Check breakout channel registry
        if channel_id in self._breakout_channels:
            session_id = self._breakout_channels[channel_id]
            # Find the mapping by session_id
            for mapping in self._mappings.values():
                if mapping.session_id == session_id:
                    return mapping

        return None

    def get_mapping_by_session(self, session_id: str) -> SessionMapping | None:
        """Find mapping by Amplifier session ID."""
        for mapping in self._mappings.values():
            if mapping.session_id == session_id:
                return mapping
        return None

    async def create_session(
        self,
        channel_id: str,
        thread_ts: str | None,
        user_id: str,
        description: str = "",
        working_dir: str | None = None,
    ) -> SessionMapping:
        """Create a new Amplifier session and map it to a Slack context.

        If thread_per_session is enabled and thread_ts is None, the bridge
        will create a new thread in the hub channel for this session.
        """
        # Resolve working directory: explicit param > config default
        effective_dir = working_dir or self._config.default_working_dir
        logger.info(
            "Creating session with working_dir=%s (source: %s)",
            effective_dir,
            "explicit" if working_dir else "config default",
        )

        # Create the backend session
        info = await self._backend.create_session(
            working_dir=effective_dir,
            bundle_name=self._config.default_bundle,
            description=description,
        )

        # Determine the conversation key
        key = f"{channel_id}:{thread_ts}" if thread_ts else channel_id

        now = datetime.now(UTC).isoformat()
        mapping = SessionMapping(
            session_id=info.session_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            project_id=info.project_id,
            description=description,
            created_by=user_id,
            created_at=now,
            last_active=now,
            working_dir=info.working_dir,
        )
        self._mappings[key] = mapping
        self._save_sessions()
        logger.info(f"Created session {info.session_id} mapped to {key}")
        return mapping

    async def connect_session(
        self,
        channel_id: str,
        thread_ts: str | None,
        user_id: str,
        working_dir: str,
        description: str = "",
        session_id: str | None = None,  # NEW — resume existing session when provided
    ) -> SessionMapping:
        """Connect a Slack context to a backend session in *working_dir*.

        When session_id is provided, resumes the existing session by replaying its
        transcript (identical to the auto-reconnect path after a server restart).
        When session_id is None, creates a fresh session in working_dir as before.

        Errors from the backend propagate unmodified — callers must handle them.
        """
        # Resume existing session or create a new one in the given directory.
        # resume_session returns None — use our known session_id directly.
        # Fail fast: call backend before mutating any local state.
        if session_id is not None:
            await self._backend.resume_session(session_id, working_dir)
            effective_session_id = session_id
            effective_project_id = ""  # no new project ID issued on resume
            effective_working_dir = working_dir
        else:
            info = await self._backend.create_session(
                working_dir=working_dir,
                bundle_name=self._config.default_bundle,
                description=description,
            )
            effective_session_id = info.session_id
            effective_project_id = info.project_id
            effective_working_dir = info.working_dir

        key = f"{channel_id}:{thread_ts}" if thread_ts else channel_id
        now = datetime.now(UTC).isoformat()

        mapping = SessionMapping(
            session_id=effective_session_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            project_id=effective_project_id,
            description=description,
            created_by=user_id,
            created_at=now,
            last_active=now,
            working_dir=effective_working_dir,
        )

        self._mappings[key] = mapping
        self._save_sessions()
        logger.info(
            "Connected session %s (in %s) mapped to %s",
            effective_session_id,
            effective_working_dir,
            key,
        )
        return mapping

    async def route_message(self, message: SlackMessage) -> str | None:
        """Route a Slack message to the appropriate Amplifier session.

        Returns the response text, or None if no session is mapped.
        Updates the last_active timestamp on the mapping.
        """
        mapping = self.get_mapping(message.channel_id, message.thread_ts)
        if mapping is None or not mapping.is_active:
            return None

        # Update activity timestamp
        mapping.last_active = datetime.now(UTC).isoformat()
        self._save_sessions()

        # Send to backend
        try:
            response = await self._backend.send_message(
                mapping.session_id, message.text
            )
            return response
        except ValueError:
            # Session is permanently dead (backend can't find or reconnect it).
            # Deactivate the mapping so the user isn't stuck in a zombie loop.
            mapping.is_active = False
            self._save_sessions()
            logger.warning(
                "Session %s is dead, deactivated mapping for %s",
                mapping.session_id,
                mapping.conversation_key,
            )
            return "Session has ended. Start a new one with `/amp new`."
        except Exception:
            logger.exception(f"Error routing message to session {mapping.session_id}")
            return "Error: Failed to get response from Amplifier session."

    async def end_session(self, channel_id: str, thread_ts: str | None = None) -> bool:
        """End the session mapped to a Slack context.

        Returns True if a session was ended, False if none was found.
        """
        mapping = self.get_mapping(channel_id, thread_ts)
        if mapping is None:
            return False

        mapping.is_active = False
        self._save_sessions()
        try:
            await self._backend.end_session(mapping.session_id)
        except (RuntimeError, ValueError, ConnectionError, OSError):
            logger.exception(f"Error ending session {mapping.session_id}")

        return True

    async def breakout_to_channel(
        self,
        channel_id: str,
        thread_ts: str,
        channel_name: str | None = None,
    ) -> SlackChannel | None:
        """Promote a thread-based session to its own channel.

        Creates a new Slack channel and remaps the session to it.
        Returns the new channel, or None if no session was found.
        """
        mapping = self.get_mapping(channel_id, thread_ts)
        if mapping is None:
            return None

        if not self._config.allow_breakout:
            raise ValueError("Channel breakout is not enabled.")

        # Generate channel name
        if channel_name is None:
            short_id = mapping.session_id[:8]
            channel_name = f"{self._config.channel_prefix}{short_id}"

        # Create the channel
        topic = f"Amplifier session {mapping.session_id[:8]}"
        if mapping.description:
            topic += f" - {mapping.description}"

        new_channel = await self._client.create_channel(channel_name, topic=topic)

        # Update mapping: remove old key, add channel-level key
        old_key = mapping.conversation_key
        self._mappings.pop(old_key, None)

        mapping.channel_id = new_channel.id
        mapping.thread_ts = None  # Now it's channel-level
        self._mappings[new_channel.id] = mapping
        self._breakout_channels[new_channel.id] = mapping.session_id
        self._save_sessions()

        # Notify in the new channel
        await self._client.post_message(
            new_channel.id,
            f"Session `{mapping.session_id[:8]}` moved to this channel."
            " Continue the conversation here.",
        )

        return new_channel

    def list_active(self) -> list[SessionMapping]:
        """List all active session mappings."""
        return [m for m in self._mappings.values() if m.is_active]

    def list_user_sessions(self, user_id: str) -> list[SessionMapping]:
        """List active sessions for a specific user."""
        return [
            m
            for m in self._mappings.values()
            if m.created_by == user_id and m.is_active
        ]

    def rekey_mapping(self, channel_id: str, thread_ts: str) -> None:
        """Re-key a bare channel mapping to a composite channel_id:thread_ts key.

        Called immediately after post_message() creates the reply thread for a
        'new' command. Without this upgrade, a second 'new' command in the same
        channel stores its session under the same bare channel_id key, silently
        overwriting the first session's routing entry (issue #54).

        Only targets the bare channel_id key. If no such key exists (e.g., the
        session was already thread-scoped), logs a warning and returns safely.
        """
        mapping = self._mappings.pop(channel_id, None)
        if mapping is None:
            logger.warning(
                f"rekey_mapping: no bare-channel mapping found for {channel_id!r}"
            )
            return

        mapping.thread_ts = thread_ts
        new_key = f"{channel_id}:{thread_ts}"
        self._mappings[new_key] = mapping
        self._save_sessions()
        logger.info(
            f"Re-keyed session {mapping.session_id} from {channel_id!r} to {new_key!r}"
        )
