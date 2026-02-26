# pyright: reportMissingImports=false
"""Socket Mode adapter for the Slack bridge.

Connects to Slack via WebSocket (no public URL needed).
Routes incoming events to the existing SlackEventHandler.

Uses a direct aiohttp WebSocket connection instead of slack_bolt,
because the slack_bolt AsyncSocketModeHandler silently drops events
in some configurations. This gives us full control over the
WebSocket lifecycle and event dispatch.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import TYPE_CHECKING, Any

import aiohttp
import httpx

if TYPE_CHECKING:
    from .config import SlackConfig
    from .events import SlackEventHandler

logger = logging.getLogger(__name__)

# Reconnect backoff
_INITIAL_BACKOFF = 1.0
_MAX_BACKOFF = 60.0
_BACKOFF_FACTOR = 2.0

# If no WebSocket frame arrives within this window, assume the connection is dead.
# Slack sends pings every ~30s, so 5 minutes of silence = definitely dead.
_RECEIVE_TIMEOUT = 300  # seconds

# Dedup window: ignore duplicate events for the same message within this window.
# Must be longer than the slowest session creation (~30s) plus Slack retry
# delays, otherwise the key expires and duplicates slip through.
_DEDUP_WINDOW_SECS = 120.0
_DEDUP_MAX_SIZE = 200


class SocketModeAdapter:
    """Bridges Slack Socket Mode events to our SlackEventHandler.

    Manages the WebSocket connection directly via aiohttp:
    - Calls apps.connections.open for a fresh WebSocket URL
    - Connects and processes frames (hello, events, ping/pong)
    - Acknowledges events and forwards them to our handler
    - Automatic reconnection with exponential backoff
    """

    def __init__(
        self,
        config: SlackConfig,
        event_handler: SlackEventHandler,
    ) -> None:
        self._config = config
        self._event_handler = event_handler
        self._task: asyncio.Task[None] | None = None
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._running = False
        self._bot_user_id: str | None = None
        # Dedup: track recently-seen message timestamps to avoid processing
        # both app_mention and message events for the same @mention.
        # Maps "channel:ts" -> monotonic time when first seen.
        self._seen_events: dict[str, float] = {}
        # Pending background event tasks — tracked so we can drain on stop()
        # and log exceptions via done callbacks.
        self._pending_tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        """Start the Socket Mode connection in the background."""
        if not self._config.app_token:
            raise ValueError(
                "SLACK_APP_TOKEN is required for Socket Mode. "
                "Generate one at https://api.slack.com/apps -> Socket Mode"
            )

        # Resolve our own bot user ID so we can filter self-messages
        self._bot_user_id = await self._resolve_bot_id()

        self._running = True
        self._task = asyncio.create_task(self._connection_loop())
        logger.info("Socket Mode adapter started")

    async def _resolve_bot_id(self) -> str | None:
        """Get the bot's own user ID via auth.test."""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    "https://slack.com/api/auth.test",
                    headers={"Authorization": f"Bearer {self._config.bot_token}"},
                )
                data = resp.json()
                if data.get("ok"):
                    bot_id = data.get("user_id")
                    logger.info(f"Bot user ID: {bot_id}")
                    return bot_id
        except (httpx.HTTPError, KeyError, ValueError):
            logger.exception("Failed to resolve bot user ID")
        return None

    async def _get_ws_url(self) -> str:
        """Call apps.connections.open to get a fresh WebSocket URL."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://slack.com/api/apps.connections.open",
                headers={
                    "Authorization": f"Bearer {self._config.app_token}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            data = resp.json()
            if not data.get("ok"):
                raise RuntimeError(f"apps.connections.open failed: {data.get('error')}")
            return data["url"]

    async def _connection_loop(self) -> None:
        """Main loop: connect, process, reconnect on failure."""
        backoff = _INITIAL_BACKOFF

        while self._running:
            try:
                url = await self._get_ws_url()
                logger.info("Connecting to Slack Socket Mode WebSocket...")

                session = aiohttp.ClientSession()
                self._session = session
                self._ws = await session.ws_connect(url)
                logger.info("WebSocket connected")

                # Reset backoff on successful connection
                backoff = _INITIAL_BACKOFF

                await self._process_frames()

            except asyncio.CancelledError:
                logger.info("Socket Mode connection cancelled")
                break
            except Exception:
                logger.exception("Socket Mode connection error")
            finally:
                await self._close_ws()

            if self._running:
                logger.info(f"Reconnecting in {backoff:.0f}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * _BACKOFF_FACTOR, _MAX_BACKOFF)

    async def _process_frames(self) -> None:
        """Read and dispatch WebSocket frames.

        Uses an explicit receive timeout instead of ``async for`` so that
        silently-dead TCP connections (common on WSL2) are detected and
        the reconnection loop can kick in.  Slack sends pings roughly
        every 30 s, so 5 minutes of silence is a reliable death signal.
        """
        assert self._ws is not None
        logger.info("[socket] Entering frame processing loop")

        while self._running and self._ws and not self._ws.closed:
            try:
                msg = await asyncio.wait_for(
                    self._ws.receive(), timeout=_RECEIVE_TIMEOUT
                )
            except TimeoutError:
                logger.warning(
                    f"[socket] No frames received in {_RECEIVE_TIMEOUT}s, "
                    "assuming dead connection"
                )
                break

            logger.debug(f"[socket] Frame: type={msg.type}")

            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    frame_type = data.get("type", "?")
                    logger.debug(f"[socket] TEXT frame: {frame_type}")
                    await self._handle_frame(data)
                except (json.JSONDecodeError, KeyError, ValueError, RuntimeError):
                    logger.exception("Error handling WebSocket frame")

            elif msg.type == aiohttp.WSMsgType.ERROR:
                logger.error(f"WebSocket error: {self._ws.exception()}")
                break

            elif msg.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.CLOSED,
            ):
                logger.info("WebSocket closed by server")
                break

        logger.info("[socket] Frame processing loop exited")

    async def _handle_frame(self, frame: dict[str, Any]) -> None:
        """Dispatch a single WebSocket frame."""
        frame_type = frame.get("type", "")

        if frame_type == "hello":
            num_conns = frame.get("num_connections", "?")
            logger.info(f"Socket Mode hello (connections: {num_conns})")

        elif frame_type == "disconnect":
            reason = frame.get("reason", "unknown")
            logger.warning(f"Socket Mode disconnect requested: {reason}")
            # Server wants us to reconnect (e.g., deploy)
            if self._ws:
                await self._ws.close()

        elif frame_type == "events_api":
            # ACK immediately so Slack doesn't retry (3 s deadline)
            await self._ack(frame)
            # Extract context for error logging before the task is created
            _payload = frame.get("payload", {})
            _event = _payload.get("event", {})
            _ctx = {
                "channel": _event.get("channel", "?"),
                "user": _event.get("user", "?"),
                "thread_ts": _event.get("thread_ts", ""),
                "text": _event.get("text", "")[:80],
            }
            task = asyncio.create_task(self._handle_event(frame))
            self._pending_tasks.add(task)

            def _done_cb(t: asyncio.Task, ctx: dict = _ctx) -> None:
                self._pending_tasks.discard(t)
                if not t.cancelled():
                    exc = t.exception()
                    if exc:
                        logger.error(
                            "[socket] Event task failed "
                            "channel=%s user=%s thread_ts=%s text=%r: %s",
                            ctx["channel"],
                            ctx["user"],
                            ctx["thread_ts"],
                            ctx["text"],
                            exc,
                            exc_info=exc,
                        )

            task.add_done_callback(_done_cb)

        elif frame_type == "interactive":
            await self._ack(frame)
            await self._handle_interactive(frame)

        elif frame_type == "slash_commands":
            await self._handle_slash_command(frame)

        # Ignore pings -- aiohttp handles pong automatically

    async def _handle_event(self, frame: dict[str, Any]) -> None:
        """Process an events_api frame."""
        # NOTE: ACK is sent by _handle_frame before this task starts.
        payload = frame.get("payload", {})
        event = payload.get("event", {})

        event_type = event.get("type", "?")
        user = event.get("user", "?")
        text = event.get("text", "")[:80]
        channel = event.get("channel", "?")
        msg_ts = event.get("ts", "")

        thread_ts = event.get("thread_ts", "")
        logger.info(
            f"[socket] Event: type={event_type} user={user} "
            f"channel={channel} thread_ts={thread_ts or 'none'} text={text!r}"
        )

        # Skip our own messages
        if user == self._bot_user_id:
            logger.debug("[socket] Skipping own message")
            return

        # Skip bot messages (subtype check)
        if event.get("subtype") == "bot_message":
            logger.debug("[socket] Skipping bot_message subtype")
            return

        # Deduplicate: Slack sends both app_mention and message events for
        # the same @mention. We only process the first one we see for a
        # given channel:ts pair.
        if msg_ts and channel:
            dedup_key = f"{channel}:{msg_ts}"
            if self._is_duplicate(dedup_key):
                logger.info(
                    f"[socket] Skipping duplicate event {event_type} for {dedup_key}"
                )
                return

        # Forward to our event handler
        handler_payload = {
            "type": "event_callback",
            "event": event,
        }
        try:
            result = await self._event_handler.handle_event_payload(handler_payload)
            logger.info(f"[socket] Handler result: {result}")
        except Exception:
            logger.exception("[socket] Error in event handler")
            raise  # re-raise so the done callback can log it with context

    async def _handle_interactive(self, frame: dict[str, Any]) -> None:
        """Process an interactive frame (button clicks, modals, etc.)."""
        payload = frame.get("payload", {})
        action_type = payload.get("type", "?")
        user = payload.get("user", {}).get("username", "?")
        actions = payload.get("actions", [])
        action_id = actions[0].get("action_id", "?") if actions else "?"

        logger.info(
            f"[socket] Interactive: type={action_type} user={user} action={action_id}"
        )

        try:
            await self._event_handler.handle_interactive_payload(payload)
        except Exception:
            logger.exception("[socket] Error in interactive handler")

    async def _handle_slash_command(self, frame: dict[str, Any]) -> None:
        """Process a slash command frame (e.g. /amp list)."""
        payload = frame.get("payload", {})
        command_name = payload.get("command", "?")
        text = payload.get("text", "")
        user = payload.get("user_name", "?")

        logger.info(f"[socket] Slash command: {command_name} text={text!r} user={user}")

        try:
            response = await self._event_handler.handle_slash_command(payload)
            # Ack with the response payload so Slack displays it inline
            await self._ack(frame, response=response)
        except Exception:
            logger.exception("[socket] Error in slash command handler")
            await self._ack(frame, response={"text": "Error processing command."})

    def _is_duplicate(self, key: str) -> bool:
        """Check if this event key was recently seen. Records it if not.

        Uses a simple dict with monotonic timestamps. Evicts stale entries
        periodically to bound memory.
        """
        now = time.monotonic()

        # Evict stale entries if the cache is getting large
        if len(self._seen_events) > _DEDUP_MAX_SIZE:
            cutoff = now - _DEDUP_WINDOW_SECS
            self._seen_events = {
                k: v for k, v in self._seen_events.items() if v > cutoff
            }

        if key in self._seen_events:
            age = now - self._seen_events[key]
            if age < _DEDUP_WINDOW_SECS:
                return True
            # Expired, treat as new

        self._seen_events[key] = now
        return False

    async def _ack(
        self, frame: dict[str, Any], response: dict[str, Any] | None = None
    ) -> None:
        """Send acknowledgement for a Socket Mode envelope.

        For slash commands and interactive payloads, ``response`` can include
        a Slack response body (text, blocks, response_type, etc.) that Slack
        will display inline.
        """
        eid = frame.get("envelope_id")
        if eid and self._ws and not self._ws.closed:
            ack_payload: dict[str, Any] = {"envelope_id": eid}
            if response is not None:
                ack_payload["payload"] = response
            await self._ws.send_json(ack_payload)

    async def _close_ws(self) -> None:
        """Close WebSocket and HTTP session."""
        if self._ws and not self._ws.closed:
            try:
                await self._ws.close()
            except OSError:
                logger.debug("Error closing WebSocket", exc_info=True)
        self._ws = None

        if self._session and not self._session.closed:
            try:
                await self._session.close()
            except OSError:
                logger.debug("Error closing HTTP session", exc_info=True)
        self._session = None

    async def stop(self) -> None:
        """Stop the Socket Mode connection.

        Drains pending event tasks (up to 30 s) before cancelling the
        main connection loop, so in-flight LLM calls complete cleanly.
        """
        self._running = False

        await self._close_ws()

        # Drain pending event tasks
        if self._pending_tasks:
            _tasks_snapshot = set(self._pending_tasks)
            _, still_pending = await asyncio.wait(_tasks_snapshot, timeout=30.0)
            for task in still_pending:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

        logger.info("Socket Mode adapter stopped")
