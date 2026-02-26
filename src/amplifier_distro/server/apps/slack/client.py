"""Slack API client abstraction.

Provides a Protocol for Slack API operations and two implementations:
- MemorySlackClient: In-memory, no network calls (testing/simulator)
- HttpSlackClient: Real Slack Web API calls (production)

The bridge always works through the SlackClient protocol, making it
fully testable without a real Slack workspace.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .models import SlackChannel


@runtime_checkable
class SlackClient(Protocol):
    """Protocol for Slack API operations."""

    async def post_message(
        self,
        channel: str,
        text: str,
        thread_ts: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
    ) -> str:
        """Post a message to a channel. Returns the message ts."""
        ...

    async def update_message(
        self,
        channel: str,
        ts: str,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
    ) -> None:
        """Update an existing message."""
        ...

    async def create_channel(self, name: str, topic: str = "") -> SlackChannel:
        """Create a new channel. Returns channel info."""
        ...

    async def get_channel_info(self, channel_id: str) -> SlackChannel | None:
        """Get channel info. Returns None if not found."""
        ...

    async def add_reaction(self, channel: str, ts: str, emoji: str) -> None:
        """Add a reaction to a message."""
        ...

    async def get_bot_user_id(self) -> str:
        """Get the bot's own user ID."""
        ...


@dataclass
class SentMessage:
    """Record of a message sent through the client (for testing)."""

    channel: str
    text: str
    thread_ts: str | None
    blocks: list[dict[str, Any]] | None
    ts: str


class MemorySlackClient:
    """In-memory Slack client for testing and simulation.

    Records all operations for inspection. No network calls.
    Implements SlackClient protocol.
    """

    def __init__(self, bot_user_id: str = "U_AMP_BOT") -> None:
        self.bot_user_id = bot_user_id
        self.sent_messages: list[SentMessage] = []
        self.updated_messages: list[dict[str, Any]] = []
        self.created_channels: dict[str, SlackChannel] = {}
        self.reactions: list[dict[str, str]] = []
        self._channels: dict[str, SlackChannel] = {}
        self._ts_counter: int = 1000000
        # Callback for when messages are sent (used by simulator)
        self.on_message_sent: Any = None

    def _next_ts(self) -> str:
        """Generate a unique Slack-style timestamp."""
        self._ts_counter += 1
        return f"{time.time():.6f}".replace(".", "")[:10] + f".{self._ts_counter:06d}"

    def seed_channel(self, channel: SlackChannel) -> None:
        """Pre-populate a channel (for test setup)."""
        self._channels[channel.id] = channel

    async def post_message(
        self,
        channel: str,
        text: str,
        thread_ts: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
    ) -> str:
        ts = self._next_ts()
        msg = SentMessage(
            channel=channel, text=text, thread_ts=thread_ts, blocks=blocks, ts=ts
        )
        self.sent_messages.append(msg)
        if self.on_message_sent:
            self.on_message_sent(msg)
        return ts

    async def update_message(
        self,
        channel: str,
        ts: str,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
    ) -> None:
        self.updated_messages.append(
            {"channel": channel, "ts": ts, "text": text, "blocks": blocks}
        )

    async def create_channel(self, name: str, topic: str = "") -> SlackChannel:
        channel_id = f"C{len(self.created_channels) + 100:06d}"
        channel = SlackChannel(id=channel_id, name=name, topic=topic)
        self.created_channels[name] = channel
        self._channels[channel_id] = channel
        return channel

    async def get_channel_info(self, channel_id: str) -> SlackChannel | None:
        return self._channels.get(channel_id)

    async def add_reaction(self, channel: str, ts: str, emoji: str) -> None:
        self.reactions.append({"channel": channel, "ts": ts, "emoji": emoji})

    async def get_bot_user_id(self) -> str:
        return self.bot_user_id


class HttpSlackClient:
    """Real Slack Web API client.

    Uses HTTP requests to the Slack API. Requires a valid bot token.
    For production use behind Tailscale or similar.

    NOTE: This is a minimal implementation. For full production use,
    consider using the official slack_sdk package.
    """

    def __init__(self, bot_token: str) -> None:
        self._token = bot_token
        self._bot_user_id: str | None = None
        self._base_url = "https://slack.com/api"

    async def _api_call(self, method: str, **kwargs: Any) -> dict[str, Any]:
        """Make a Slack API call."""
        import httpx

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self._base_url}/{method}",
                headers={"Authorization": f"Bearer {self._token}"},
                json=kwargs,
            )
            data = response.json()
            if not data.get("ok"):
                raise RuntimeError(f"Slack API error: {data.get('error', 'unknown')}")
            return data

    async def post_message(
        self,
        channel: str,
        text: str,
        thread_ts: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
    ) -> str:
        kwargs: dict[str, Any] = {"channel": channel, "text": text}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        if blocks:
            kwargs["blocks"] = blocks
        result = await self._api_call("chat.postMessage", **kwargs)
        return result["ts"]

    async def update_message(
        self,
        channel: str,
        ts: str,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {"channel": channel, "ts": ts, "text": text}
        if blocks:
            kwargs["blocks"] = blocks
        await self._api_call("chat.update", **kwargs)

    async def create_channel(self, name: str, topic: str = "") -> SlackChannel:
        result = await self._api_call("conversations.create", name=name)
        channel_id = result["channel"]["id"]
        if topic:
            await self._api_call(
                "conversations.setTopic", channel=channel_id, topic=topic
            )
        return SlackChannel(id=channel_id, name=name, topic=topic)

    async def get_channel_info(self, channel_id: str) -> SlackChannel | None:
        try:
            result = await self._api_call("conversations.info", channel=channel_id)
            ch = result["channel"]
            return SlackChannel(
                id=ch["id"],
                name=ch.get("name", ""),
                topic=ch.get("topic", {}).get("value", ""),
            )
        except RuntimeError:
            return None

    async def add_reaction(self, channel: str, ts: str, emoji: str) -> None:
        await self._api_call("reactions.add", channel=channel, timestamp=ts, name=emoji)

    async def get_bot_user_id(self) -> str:
        if self._bot_user_id is None:
            result = await self._api_call("auth.test")
            self._bot_user_id = result["user_id"]
        assert self._bot_user_id is not None
        return self._bot_user_id
